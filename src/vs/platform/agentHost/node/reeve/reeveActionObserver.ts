/*--------------------------------------------------------------------------------
 * Copyright (c) 2026 Ankesh Kumar.
 * Licensed under the MIT License. See License.txt in the project root for
 * license information.
 *--------------------------------------------------------------------------------*/

import { generateUuid } from '../../../../base/common/uuid.js';
import type { ReeveMemoryItem } from './reeveClient.js';

/**
 * Categories of actions observed during agent tool invocations.
 */
export enum ActionCategory {
	FileEdit = 'file_edit',
	FileCreate = 'file_create',
	FileDelete = 'file_delete',
	ShellCommand = 'shell_command',
	TestRun = 'test_run',
	CodebaseSearch = 'codebase_search',
	Internal = 'internal',
	Other = 'other',
}

export type ReeveActionType = 'edit' | 'create' | 'delete' | 'command' | 'test' | 'read' | 'other';

/**
 * Agent-provider agnostic action event emitted by any agent harness (Copilot, Claude, Codex, etc.).
 */
export interface ReeveActionEvent {
	readonly harness: string;
	readonly sessionId: string;
	readonly actionId?: string;
	readonly type: ReeveActionType;
	readonly toolName?: string;
	readonly target?: string;
	readonly command?: string;
	readonly diff?: string;
	readonly content?: string;
	readonly input?: unknown;
	readonly result?: unknown;
	readonly success?: boolean;
	readonly isDestructive?: boolean;
	readonly timestamp?: number;
}

export interface ActionEvidence {
	readonly type: ReeveActionType;
	readonly target?: string;
	readonly command?: string;
	readonly diff?: string;
	readonly content?: string;
	readonly result?: unknown;
	readonly success?: boolean;
}

export interface ActionContext {
	readonly phase: 'before' | 'after';
	readonly type: ReeveActionType;
	readonly target?: string;
	readonly command?: string;
	readonly diff?: string;
	readonly content?: string;
	readonly result?: unknown;
	readonly userRequest: string;
	readonly reeveMemory?: string;
	readonly codeContext?: string;
	readonly agentResponse?: string;
	readonly priorExplanation?: string;
	readonly actions?: readonly ActionEvidence[];
}

/**
 * Information captured from a single agent tool invocation.
 */
export interface ObservedAction {
	readonly id: string;
	readonly category: ActionCategory;
	readonly toolName: string;
	readonly harness?: string;
	readonly targetResource?: string;
	details?: Record<string, any>;
	readonly timestamp: number;
	isDestructive?: boolean;
	executed?: boolean;
	success?: boolean;
}

/**
 * The resulting human-centered explanation produced for the developer.
 */
export interface ActionExplanation {
	readonly summary: string;
	readonly actions?: readonly ObservedAction[];
	readonly episodeText?: string;
}

/**
 * Interface for tracking actions across a single chat interaction session/turn.
 */
export interface ISessionActionObserver {
	recordBeforeAction(event: ReeveActionEvent): { action: ObservedAction };
	recordAfterAction(event: ReeveActionEvent): void;
	recordAction(event: ReeveActionEvent): ObservedAction | undefined;
	getActions(): readonly ObservedAction[];
	hasFileDeletion(): boolean;
	getUserRequest(): string;
	isMeaningfulAction(action: ObservedAction): boolean;
}

export class SessionActionObserver implements ISessionActionObserver {
	private readonly actions: ObservedAction[] = [];
	private readonly activeActions = new Map<string, ObservedAction>();

	constructor(
		public readonly sessionId: string,
		private readonly userRequest: string = ''
	) { }

	recordBeforeAction(event: ReeveActionEvent): { action: ObservedAction } {
		const action = this.createObservedActionFromEvent(event);
		if (event.actionId) {
			this.activeActions.set(event.actionId, action);
		}
		this.actions.push(action);
		return { action };
	}

	recordAfterAction(event: ReeveActionEvent): void {
		const action = event.actionId ? this.activeActions.get(event.actionId) : this.actions[this.actions.length - 1];
		if (action) {
			action.executed = true;
			action.success = event.success ?? true;
			if (event.result !== undefined) {
				action.details = { ...action.details, result: event.result };
			}
			if (event.diff) {
				action.details = { ...action.details, diff: event.diff };
			}
			if (event.actionId) {
				this.activeActions.delete(event.actionId);
			}
		}
	}

	recordAction(event: ReeveActionEvent): ObservedAction | undefined {
		const { action } = this.recordBeforeAction(event);
		this.recordAfterAction(event);
		return action;
	}

	getActions(): readonly ObservedAction[] {
		return this.actions;
	}

	hasFileDeletion(): boolean {
		return this.actions.some(action => action.category === ActionCategory.FileDelete || (action.details?.isDestructive && action.category === ActionCategory.ShellCommand));
	}

	getUserRequest(): string {
		return this.userRequest;
	}

	isMeaningfulAction(action: ObservedAction): boolean {
		return [
			ActionCategory.FileEdit,
			ActionCategory.FileCreate,
			ActionCategory.FileDelete,
			ActionCategory.ShellCommand,
			ActionCategory.TestRun,
		].includes(action.category);
	}

	private createObservedActionFromEvent(event: ReeveActionEvent): ObservedAction {
		const category = this.actionCategoryFromType(event.type);
		return {
			id: event.actionId || generateUuid(),
			category,
			toolName: event.toolName || event.type,
			harness: event.harness,
			targetResource: event.target || event.command,
			details: {
				type: event.type,
				target: event.target,
				command: event.command,
				diff: event.diff,
				content: event.content,
				input: event.input,
				isDestructive: event.isDestructive,
			},
			timestamp: event.timestamp || Date.now(),
			isDestructive: event.isDestructive,
		};
	}

	private actionCategoryFromType(type: ReeveActionEvent['type']): ActionCategory {
		switch (type) {
			case 'edit':
				return ActionCategory.FileEdit;
			case 'create':
				return ActionCategory.FileCreate;
			case 'delete':
				return ActionCategory.FileDelete;
			case 'command':
				return ActionCategory.ShellCommand;
			case 'test':
				return ActionCategory.TestRun;
			case 'read':
				return ActionCategory.CodebaseSearch;
			default:
				return ActionCategory.Other;
		}
	}
}
