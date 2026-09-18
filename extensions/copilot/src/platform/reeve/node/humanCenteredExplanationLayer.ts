/*--------------------------------------------------------------------------------
 * Copyright (c) 2026 Ankesh Kumar.
 * Licensed under the MIT License. See License.txt in the project root for
 * license information.
 *--------------------------------------------------------------------------------*/

import {
	ChangeImpactLevel,
	ISessionActionObserver,
	ActionExplanation,
	ObservedAction,
} from '../common/reeveActionObserver';
import { ReeveMemoryItem } from '../common/reeveClient';
import { SessionActionObserver } from './reeveActionObserver';

export interface IExplanationStream {
	markdown(value: string): void;
	progress?(value: string): void;
}

/**
 * Human-Centered Change Explanation layer for Reeve.
 *
 * Observes meaningful coding actions from the existing Copilot agent,
 * explains consequential actions BEFORE they execute (prior to approval),
 * and explains results AFTER execution in natural, collegial engineering language.
 */
export class HumanCenteredExplanationLayer {
	private readonly activeObservers = new Map<string, SessionActionObserver>();
	private readonly sessionStreams = new Map<string, IExplanationStream>();

	/**
	 * Starts observing actions for an active chat turn / interaction.
	 */
	startSession(sessionId: string, stream?: IExplanationStream): ISessionActionObserver {
		const observer = new SessionActionObserver(sessionId);
		this.activeObservers.set(sessionId, observer);
		if (stream) {
			this.sessionStreams.set(sessionId, stream);
		}
		return observer;
	}

	/**
	 * Retrieves the active observer for a session.
	 */
	getSessionObserver(sessionId: string): SessionActionObserver | undefined {
		return this.activeObservers.get(sessionId);
	}

	/**
	 * Intercepts tool invocation BEFORE execution.
	 * If the action is consequential/significant, streams a pre-action explanation
	 * before confirmation/execution.
	 */
	onBeforeToolAction(
		toolName: string,
		input: any,
		sessionId?: string,
		recalledMemories: readonly ReeveMemoryItem[] = []
	): { action: ObservedAction; preExplanation?: string } | undefined {
		const resolvedSessionId = sessionId || this.getLatestSessionId();
		if (!resolvedSessionId) {
			return undefined;
		}

		const observer = this.activeObservers.get(resolvedSessionId);
		if (!observer) {
			return undefined;
		}

		const result = observer.recordBeforeToolInvocation(toolName, input, recalledMemories);
		if (result.preExplanation) {
			const stream = this.sessionStreams.get(resolvedSessionId);
			if (stream) {
				try {
					stream.markdown(`\n\n${result.preExplanation}\n\n`);
				} catch {
					// fail-safe
				}
			}
		}

		return result;
	}

	/**
	 * Intercepts tool invocation AFTER execution.
	 */
	onAfterToolAction(
		actionId: string,
		result?: any,
		success: boolean = true,
		sessionId?: string
	): void {
		const resolvedSessionId = sessionId || this.getLatestSessionId();
		if (!resolvedSessionId) {
			return;
		}

		const observer = this.activeObservers.get(resolvedSessionId);
		observer?.recordAfterToolInvocation(actionId, result, success);
	}

	/**
	 * Finalizes the session, evaluates whether the agent explained its work adequately,
	 * and streams a natural human-centered post-change explanation if needed.
	 */
	async finalizeSession(
		sessionId: string,
		agentResponseText: string,
		stream?: IExplanationStream,
		recalledMemories: readonly ReeveMemoryItem[] = []
	): Promise<ActionExplanation | undefined> {
		const observer = this.activeObservers.get(sessionId);
		if (!observer) {
			return undefined;
		}

		try {
			const impact = observer.getOverallImpact();
			// Trivial changes need no extra explanation
			if (impact === ChangeImpactLevel.Trivial && (!recalledMemories || recalledMemories.length === 0)) {
				return undefined;
			}

			// If the agent already naturally explained the change in its response, do not duplicate
			if (observer.isExplanationAdequate(agentResponseText)) {
				return undefined;
			}

			// Generate the human-centered post-action explanation
			const explanation = observer.generateExplanation(agentResponseText, recalledMemories);
			const targetStream = stream || this.sessionStreams.get(sessionId);
			if (!explanation || !targetStream) {
				return explanation;
			}

			// Render the explanation into the response stream
			this.renderExplanation(explanation, targetStream);
			return explanation;
		} finally {
			this.activeObservers.delete(sessionId);
			this.sessionStreams.delete(sessionId);
		}
	}

	private getLatestSessionId(): string | undefined {
		const keys = Array.from(this.activeObservers.keys());
		return keys.length > 0 ? keys[keys.length - 1] : undefined;
	}

	/**
	 * Formats and renders the explanation cleanly into the stream in natural prose
	 * without rigid templates or AI status dashboards.
	 */
	private renderExplanation(explanation: ActionExplanation, stream: IExplanationStream): void {
		const sentences: string[] = [];

		if (explanation.afterExplanation) {
			sentences.push(explanation.afterExplanation);
		} else if (explanation.summary) {
			sentences.push(explanation.summary);
		}

		if (explanation.details && explanation.details.length > 0) {
			sentences.push(explanation.details.join(' '));
		}

		if (explanation.relatedReeveDecisions && explanation.relatedReeveDecisions.length > 0) {
			sentences.push(explanation.relatedReeveDecisions.join(' '));
		}

		if (explanation.uncertainty) {
			sentences.push(explanation.uncertainty);
		}

		if (sentences.length > 0) {
			stream.markdown(`\n\n${sentences.join(' ')}\n\n`);
		}
	}
}
