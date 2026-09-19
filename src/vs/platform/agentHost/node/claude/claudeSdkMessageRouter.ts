/*---------------------------------------------------------------------------------------------
 *  Copyright (c) Microsoft Corporation. All rights reserved.
 *  Licensed under the MIT License. See License.txt in the project root for license information.
 *--------------------------------------------------------------------------------------------*/

import type { PermissionMode, SDKMessage } from '@anthropic-ai/claude-agent-sdk';
import { Emitter, Event } from '../../../../base/common/event.js';
import { Disposable, IReference } from '../../../../base/common/lifecycle.js';
import { URI } from '../../../../base/common/uri.js';
import { IInstantiationService } from '../../../instantiation/common/instantiation.js';
import { ILogService } from '../../../log/common/log.js';
import { AgentSignal } from '../../common/agent.js';
import type { IAgentHostClientTelemetryContext } from '../../common/agentHostTelemetry.js';
import { ISessionDatabase } from '../../common/sessionDataService.js';
import { ActionType } from '../../common/state/sessionActions.js';
import { ResponsePartKind } from '../../common/state/sessionState.js';
import { ClaudeActionAdapter } from '../reeve/reeveAdapters.js';
import { HumanCenteredExplanationLayer } from '../reeve/humanCenteredExplanationLayer.js';
import { ClaudeFileEditObserver } from './claudeFileEditObserver.js';
import { ClaudeMapperState, mapSDKMessageToAgentSignals } from './claudeMapSessionEvents.js';
import type { SubagentRegistry } from './claudeSubagentRegistry.js';

interface IClaudeSdkMessageContext {
	readonly turnDuration?: number;
	readonly mode?: PermissionMode;
	readonly clientContext?: IAgentHostClientTelemetryContext;
}

/**
 * Per-message router. Awaits file-edit observation for `type: 'user'`
 * messages so the cached edit lands before {@link mapSDKMessageToAgentSignals}
 * reads it via `state.takeFileEdit`, then fires mapped signals on
 * {@link onDidProduceSignal}. Mapper failures are logged but never thrown.
 *
 * Owns the per-session {@link ClaudeFileEditObserver} (Phase 8) and
 * {@link ClaudeMapperState} (Phase 7) — both are private to the
 * message-handling pipeline and have no other consumers. Phase 12
 * subagent correlation state lives on {@link IClaudeSubagentResolver}
 * (host-singleton, keyed by parent session URI), which the router
 * forwards into every mapper invocation.
 */
export class ClaudeSdkMessageRouter extends Disposable {
	private readonly _onDidProduceSignal = this._register(new Emitter<AgentSignal>());
	readonly onDidProduceSignal: Event<AgentSignal> = this._onDidProduceSignal.event;

	private readonly _editObserver: ClaudeFileEditObserver;
	private readonly _mapperState = new ClaudeMapperState();
	private readonly _explanationLayer: HumanCenteredExplanationLayer;

	private _clientToolOwner: ((toolName: string) => string | undefined) | undefined;

	constructor(
		private readonly _chatChannelUri: URI,
		resource: URI,
		dbRef: IReference<ISessionDatabase>,
		private readonly _subagents: SubagentRegistry,
		clientToolOwner: ((toolName: string) => string | undefined) | undefined = undefined,
		@IInstantiationService instantiationService: IInstantiationService,
		@ILogService private readonly _logService: ILogService,
		explanationLayer?: HumanCenteredExplanationLayer,
	) {
		super();
		this._clientToolOwner = clientToolOwner;
		this._explanationLayer = explanationLayer ?? new HumanCenteredExplanationLayer();
		this._editObserver = this._register(
			instantiationService.createInstance(ClaudeFileEditObserver, resource.toString(), dbRef),
		);
	}

	get explanationLayer(): HumanCenteredExplanationLayer {
		return this._explanationLayer;
	}

	setClientToolOwner(clientToolOwner: ((toolName: string) => string | undefined) | undefined): void {
		this._clientToolOwner = clientToolOwner;
	}

	async handle(message: SDKMessage, turnId: string | undefined, context?: IClaudeSdkMessageContext): Promise<void> {
		if (message.type === 'assistant') {
			this._editObserver.observeAssistant(message, context?.mode, context?.clientContext);
			this._observeReeveAssistant(message, turnId);
		} else if (message.type === 'user' && turnId !== undefined) {
			await this._editObserver.observeUser(message, turnId, this._mapperState);
			this._observeReeveUser(message);
		} else if (message.type === 'result') {
			void this._explanationLayer.finalizeSession(this._chatChannelUri.toString()).then(res => {
				if (res?.summary && turnId) {
					this._onDidProduceSignal.fire({
						kind: 'action',
						resource: this._chatChannelUri,
						action: {
							type: ActionType.ChatResponsePart,
							turnId,
							part: {
								kind: ResponsePartKind.Markdown,
								id: `reeve-summary-${Date.now()}`,
								content: `\n\n---\n**Reeve Session Summary**:\n${res.summary}\n`,
							},
						},
					});
				}
			}).catch(err => {
				this._logService.debug(`[ClaudeSdkMessageRouter] Reeve finalizeSession error: ${err}`);
			});
		}
		if (turnId === undefined) {
			return;
		}
		try {
			const signals = mapSDKMessageToAgentSignals(
				message,
				this._chatChannelUri,
				turnId,
				this._mapperState,
				this._logService,
				this._subagents,
				this._clientToolOwner,
				context?.turnDuration,
			);
			for (const signal of signals) {
				this._onDidProduceSignal.fire(signal);
			}
		} catch (mapperErr) {
			this._logService.warn(`[ClaudeSdkMessageRouter] mapper threw, skipping message: ${mapperErr}`);
		}
	}

	private _observeReeveAssistant(message: Extract<SDKMessage, { type: 'assistant' }>, turnId: string | undefined): void {
		const content = message.message.content;
		if (!Array.isArray(content)) {
			return;
		}
		for (const block of content) {
			if (block.type === 'tool_use') {
				const event = ClaudeActionAdapter.toEvent(block.name, block.input, this._chatChannelUri.toString(), block.id);
				this._logService.info(`[Reeve:Claude] Observed tool_use "${block.name}" (id: ${block.id}) -> ReeveActionEvent(type: ${event.type}, target: ${event.target || event.command || 'none'})`);
				void this._explanationLayer.onBeforeAction(event).then(res => {
					if (res?.preExplanation) {
						this._logService.info(`[Reeve:Claude] Pre-action explanation: "${res.preExplanation}"`);
						if (turnId) {
							this._onDidProduceSignal.fire({
								kind: 'action',
								resource: this._chatChannelUri,
								action: {
									type: ActionType.ChatResponsePart,
									turnId,
									part: {
										kind: ResponsePartKind.Markdown,
										id: `reeve-explanation-${block.id}`,
										content: `\n\n> 💡 **Reeve Explanation**: ${res.preExplanation}\n\n`,
									},
								},
							});
						}
					}
				}).catch(err => {
					this._logService.debug(`[ClaudeSdkMessageRouter] Reeve onBeforeAction error: ${err}`);
				});
			}
		}
	}

	private _observeReeveUser(message: Extract<SDKMessage, { type: 'user' }>): void {
		const content = message.message.content;
		if (!Array.isArray(content)) {
			return;
		}
		for (const block of content) {
			if (block.type === 'tool_result') {
				const isError = block.is_error === true;
				this._logService.info(`[Reeve:Claude] Observed tool_result (id: ${block.tool_use_id}, success: ${!isError}) -> Reeve after-action recorded`);
				this._explanationLayer.onAfterAction({
					harness: 'claude',
					sessionId: this._chatChannelUri.toString(),
					actionId: block.tool_use_id,
					type: 'other',
					result: block.content,
					success: !isError,
					timestamp: Date.now(),
				});
			}
		}
	}
}
