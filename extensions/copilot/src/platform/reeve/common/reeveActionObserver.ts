/*--------------------------------------------------------------------------------
 * Copyright (c) 2026 Ankesh Kumar.
 * Licensed under the MIT License. See License.txt in the project root for
 * license information.
 *--------------------------------------------------------------------------------*/

import { ReeveMemoryItem } from './reeveClient';

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
 * Agent-provider agnostic action event emitted by any agent harness (Copilot, Claude, etc.).
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

export interface ActionEvidence {
	readonly type: ReeveActionType;
	readonly target?: string;
	readonly command?: string;
	readonly diff?: string;
	readonly content?: string;
	readonly result?: unknown;
	readonly success?: boolean;
}

/**
 * Information captured from a single agent tool invocation.
 */
export interface ObservedAction {
	readonly id: string;
	readonly category: ActionCategory;
	readonly toolName: string;
	readonly harness?: string;
	readonly targetResource?: string; // Target file or command
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
	/**
	 * Records a harness-agnostic action event before execution.
	 */
	recordBeforeAction(event: ReeveActionEvent): { action: ObservedAction };

	/**
	 * Records a harness-agnostic action event after execution.
	 */
	recordAfterAction(event: ReeveActionEvent): void;

	/**
	 * Records a harness-agnostic action event directly.
	 */
	recordAction(event: ReeveActionEvent): ObservedAction | undefined;

	/**
	 * Legacy Copilot tool hook adapter.
	 */
	recordBeforeToolInvocation(
		toolName: string,
		input: any,
		recalledMemories?: readonly ReeveMemoryItem[]
	): { action: ObservedAction };

	/**
	 * Legacy Copilot tool hook adapter.
	 */
	recordAfterToolInvocation(
		actionId: string,
		result?: any,
		success?: boolean
	): void;

	/**
	 * Legacy Copilot tool hook adapter.
	 */
	recordToolInvocation(
		toolName: string,
		input: any,
		result?: any,
		success?: boolean
	): ObservedAction | undefined;

	/**
	 * Returns all actions observed in this session.
	 */
	getActions(): readonly ObservedAction[];

	hasFileDeletion(): boolean;

	getUserRequest(): string;
	isMeaningfulAction(action: ObservedAction): boolean;
}
