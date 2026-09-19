/*--------------------------------------------------------------------------------
 * Copyright (c) 2026 Ankesh Kumar.
 * Licensed under the MIT License. See License.txt in the project root for
 * license information.
 *--------------------------------------------------------------------------------*/

import { ActionContext, ActionEvidence, ActionExplanation, ISessionActionObserver, ObservedAction, ReeveActionEvent } from './reeveActionObserver.js';
import { ReeveMemoryItem } from './reeveClient.js';
import { HumanExplanationService, IHumanExplanationModel } from './humanExplanationService.js';
import { SessionActionObserver } from './reeveActionObserver.js';

export interface IExplanationStream {
	markdown(value: string): void;
	progress?(value: string): void;
}

export class HumanCenteredExplanationLayer {
	private readonly activeObservers = new Map<string, SessionActionObserver>();
	private readonly sessionStreams = new Map<string, IExplanationStream>();
	private readonly sessionMemories = new Map<string, readonly ReeveMemoryItem[]>();
	private readonly preExplanations = new Map<string, string>();

	constructor(private readonly explanationService: IHumanExplanationModel = new HumanExplanationService()) { }

	startSession(sessionId: string, stream?: IExplanationStream, userRequest = ''): ISessionActionObserver {
		const observer = new SessionActionObserver(sessionId, userRequest);
		this.activeObservers.set(sessionId, observer);
		if (stream) {
			this.sessionStreams.set(sessionId, stream);
		}
		this.sessionMemories.set(sessionId, []);
		return observer;
	}

	getSessionObserver(sessionId: string): SessionActionObserver | undefined {
		return this.activeObservers.get(sessionId);
	}

	/**
	 * Agent-provider agnostic action hook: called before any agent harness executes an action.
	 */
	async onBeforeAction(event: ReeveActionEvent, recalledMemories: readonly ReeveMemoryItem[] = []): Promise<{ action: ObservedAction; preExplanation?: string } | undefined> {
		const targetSessionId = event.sessionId || (this.activeObservers.size === 1 ? this.activeObservers.keys().next().value : undefined);
		if (!targetSessionId) {
			return undefined;
		}
		let observer = this.activeObservers.get(targetSessionId);
		if (!observer) {
			observer = this.startSession(targetSessionId) as SessionActionObserver;
		}

		const { action } = observer.recordBeforeAction(event);
		this.sessionMemories.set(targetSessionId, recalledMemories);
		if (!observer.isMeaningfulAction(action)) {
			return { action };
		}
		const explanation = await this.explanationService.explain(this.buildContext(observer, action, recalledMemories, 'before'));
		if (explanation) {
			this.preExplanations.set(targetSessionId, explanation);
		}
		this.render(explanation, targetSessionId);
		return { action, preExplanation: explanation };
	}

	/**
	 * Agent-provider agnostic action hook: called after any agent harness finishes an action.
	 */
	onAfterAction(event: ReeveActionEvent): void {
		const targetSessionId = event.sessionId || (this.activeObservers.size === 1 ? this.activeObservers.keys().next().value : undefined);
		if (targetSessionId) {
			this.activeObservers.get(targetSessionId)?.recordAfterAction(event);
		}
	}

	async finalizeSession(sessionId: string, agentResponseText: string = '', stream?: IExplanationStream, recalledMemories: readonly ReeveMemoryItem[] = []): Promise<ActionExplanation | undefined> {
		const observer = this.activeObservers.get(sessionId);
		if (!observer) {
			return undefined;
		}
		if (stream) {
			this.sessionStreams.set(sessionId, stream);
		}
		const memories = recalledMemories.length > 0 ? recalledMemories : (this.sessionMemories.get(sessionId) || []);
		const allActions = observer.getActions();
		const meaningfulActions = allActions.filter(action => observer.isMeaningfulAction(action));
		let explanationText: string | undefined;

		if (meaningfulActions.length > 0) {
			const priorExplanation = this.preExplanations.get(sessionId);
			const context = this.buildContext(observer, meaningfulActions[meaningfulActions.length - 1], memories, 'after', agentResponseText, priorExplanation, meaningfulActions);
			explanationText = await this.explanationService.explain(context);
		}

		if (explanationText) {
			this.render(explanationText, sessionId);
		}

		const userRequest = observer.getUserRequest();
		const episodeText = this.formatTurnEpisode(userRequest, allActions, agentResponseText, explanationText);

		const explanation: ActionExplanation = {
			summary: explanationText || '',
			actions: allActions,
			episodeText,
		};
		this.activeObservers.delete(sessionId);
		this.sessionStreams.delete(sessionId);
		this.sessionMemories.delete(sessionId);
		this.preExplanations.delete(sessionId);
		return explanation;
	}

	public formatTurnEpisode(
		userRequest: string,
		actions: readonly ObservedAction[],
		agentResponse: string,
		explanation?: string
	): string {
		const parts: string[] = [];

		if (userRequest && userRequest.trim()) {
			parts.push(`User Request:\n${userRequest.trim()}`);
		}

		if (actions && actions.length > 0) {
			const actionSummaries = actions.map(act => {
				const tool = act.toolName || act.category;
				const status = act.success === false ? 'FAILED' : 'SUCCESS';
				const target = act.targetResource ? ` on "${act.targetResource}"` : '';
				const cmd = act.details?.command ? `: \`${act.details.command}\`` : '';
				let line = `- Tool [${tool}] (${status})${target}${cmd}`;

				const resultStr = this.extractResultString(act.details?.result);
				if (resultStr && resultStr.trim()) {
					const truncated = resultStr.length > 800 ? `${resultStr.slice(0, 800)}...` : resultStr.trim();
					line += `\n  Output: ${truncated.replace(/\r?\n/g, ' ')}`;
				}
				if (act.details?.diff) {
					const diffStr = String(act.details.diff).trim();
					const truncatedDiff = diffStr.length > 400 ? `${diffStr.slice(0, 400)}...` : diffStr;
					line += `\n  Diff: ${truncatedDiff.replace(/\r?\n/g, ' ')}`;
				}
				return line;
			}).join('\n');

			parts.push(`Executed Actions & Tool Outputs:\n${actionSummaries}`);
		}

		if (agentResponse && agentResponse.trim()) {
			const cleanResponse = agentResponse.length > 2000
				? `${agentResponse.slice(0, 2000)}... (truncated)`
				: agentResponse.trim();
			parts.push(`Agent Response:\n${cleanResponse}`);
		}

		if (explanation && explanation.trim()) {
			parts.push(`Human Explanation:\n${explanation.trim()}`);
		}

		return parts.join('\n\n');
	}

	private extractResultString(result: any): string {
		if (!result) {
			return '';
		}
		if (typeof result === 'string') {
			return result;
		}
		if (result instanceof Error) {
			return result.message;
		}
		if (typeof result === 'object') {
			if (Array.isArray(result.content)) {
				const parts: string[] = [];
				for (const p of result.content) {
					if (typeof p?.value === 'string') {
						parts.push(p.value);
					} else if (p?.value && typeof p.value === 'object') {
						try { parts.push(JSON.stringify(p.value)); } catch { /* ignore */ }
					} else if (typeof p?.text === 'string') {
						parts.push(p.text);
					}
				}
				if (parts.length > 0) {
					return parts.join('\n');
				}
			}
			if (typeof result.stdout === 'string' || typeof result.stderr === 'string') {
				return `${result.stdout || ''}${result.stderr ? `\nStderr: ${result.stderr}` : ''}`.trim();
			}
			if (typeof result.output === 'string') {
				return result.output;
			}
			if (typeof result.message === 'string') {
				return result.message;
			}
			try {
				return JSON.stringify(result);
			} catch {
				return String(result);
			}
		}
		return String(result);
	}

	private render(text: string | undefined, sessionId: string): void {
		if (!text) {
			return;
		}
		const stream = this.sessionStreams.get(sessionId);
		if (stream) {
			stream.markdown(`\n\n*${text}*\n\n`);
		}
	}

	private buildContext(
		observer: SessionActionObserver,
		action: ObservedAction | undefined,
		memories: readonly ReeveMemoryItem[],
		phase: ActionContext['phase'],
		agentResponse?: string,
		priorExplanation?: string,
		actions: readonly ObservedAction[] = [action].filter((candidate): candidate is ObservedAction => !!candidate)
	): ActionContext {
		const memory = memories
			.map(item => item.isHistorical ? `Historical (superseded): ${item.content}` : item.content)
			.join('\n');

		return {
			phase,
			type: (action?.details?.type as any) || 'other',
			target: action?.targetResource || action?.details?.target,
			command: action?.details?.command,
			diff: action?.details?.diff,
			content: action?.details?.content,
			result: action?.details?.result,
			userRequest: observer.getUserRequest(),
			reeveMemory: memory || undefined,
			agentResponse,
			priorExplanation,
			actions: actions.map(this.toEvidence),
		};
	}

	private toEvidence(action: ObservedAction): ActionEvidence {
		return {
			type: (action.details?.type as any) || 'other',
			target: action.targetResource || action.details?.target,
			command: action.details?.command,
			diff: action.details?.diff,
			content: action.details?.content,
			result: action.details?.result,
			success: action.success,
		};
	}
}
