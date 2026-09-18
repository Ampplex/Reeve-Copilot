/*--------------------------------------------------------------------------------
 * Copyright (c) 2026 Ankesh Kumar.
 * Licensed under the MIT License. See License.txt in the project root for
 * license information.
 *--------------------------------------------------------------------------------*/

import { ActionCategory, ISessionActionObserver, ObservedAction, ReeveActionEvent } from '../common/reeveActionObserver';
import { CopilotActionAdapter } from '../common/reeveAdapters';
import { ReeveMemoryItem } from '../common/reeveClient';

export class SessionActionObserver implements ISessionActionObserver {
	private readonly actions: ObservedAction[] = [];
	private counter = 0;

	constructor(public readonly sessionId: string, private readonly userRequest = '') { }

	/**
	 * Agent-provider agnostic hook: records any harness action event before execution.
	 */
	recordBeforeAction(event: ReeveActionEvent): { action: ObservedAction } {
		const action = this.createObservedActionFromEvent(event);
		this.actions.push(action);
		return { action };
	}

	/**
	 * Agent-provider agnostic hook: records action completion result.
	 */
	recordAfterAction(event: ReeveActionEvent): void {
		const action = this.actions.find(candidate => candidate.id === event.actionId);
		if (action) {
			action.executed = true;
			action.success = event.success ?? true;
			action.details = { ...action.details, result: event.result };
		}
	}

	recordAction(event: ReeveActionEvent): ObservedAction | undefined {
		const { action } = this.recordBeforeAction(event);
		this.recordAfterAction({ ...event, actionId: action.id });
		return action;
	}

	/**
	 * Copilot tool-call adapter for backwards compatibility.
	 */
	recordBeforeToolInvocation(toolName: string, input: any, _recalledMemories: readonly ReeveMemoryItem[] = []): { action: ObservedAction } {
		const event = CopilotActionAdapter.toEvent(toolName, input, this.sessionId);
		return this.recordBeforeAction(event);
	}

	recordAfterToolInvocation(actionId: string, result?: any, success = true): void {
		this.recordAfterAction({
			harness: 'copilot',
			sessionId: this.sessionId,
			actionId,
			type: 'other',
			result,
			success,
		});
	}

	recordToolInvocation(toolName: string, input: any, result?: any, success = true): ObservedAction | undefined {
		const event = CopilotActionAdapter.toEvent(toolName, input, this.sessionId);
		return this.recordAction({ ...event, result, success });
	}

	getActions(): readonly ObservedAction[] {
		return [...this.actions];
	}

	hasFileDeletion(): boolean {
		return this.actions.some(action => action.category === ActionCategory.FileDelete);
	}

	isMeaningfulAction(action: ObservedAction): boolean {
		return action.category === ActionCategory.FileEdit ||
			action.category === ActionCategory.FileCreate ||
			action.category === ActionCategory.FileDelete ||
			action.category === ActionCategory.ShellCommand;
	}

	getUserRequest(): string {
		return this.userRequest;
	}

	private createObservedActionFromEvent(event: ReeveActionEvent): ObservedAction {
		const actionId = event.actionId || `act_${++this.counter}_${Date.now()}`;
		const category = this.actionCategoryFromType(event.type);
		const toolName = event.toolName || event.type;
		const targetResource = event.target;
		const details: Record<string, any> = {
			command: event.command,
			diff: event.diff,
			content: event.content,
			result: event.result,
			input: event.input,
		};

		return {
			id: actionId,
			category,
			toolName,
			harness: event.harness,
			targetResource,
			details,
			timestamp: event.timestamp || Date.now(),
			isDestructive: event.isDestructive,
			executed: event.result !== undefined,
			success: event.success,
		};
	}

	private actionCategoryFromType(type: ReeveActionEvent['type']): ActionCategory {
		switch (type) {
			case 'edit': return ActionCategory.FileEdit;
			case 'create': return ActionCategory.FileCreate;
			case 'delete': return ActionCategory.FileDelete;
			case 'command': return ActionCategory.ShellCommand;
			case 'test': return ActionCategory.TestRun;
			case 'read': return ActionCategory.CodebaseSearch;
			default: return ActionCategory.Other;
		}
	}
}
