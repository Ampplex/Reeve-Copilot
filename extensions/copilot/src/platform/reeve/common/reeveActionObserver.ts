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

export interface ActionContext {
	readonly phase: 'before' | 'after';
	readonly type: 'edit' | 'create' | 'delete' | 'command' | 'test' | 'other';
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
	readonly type: ActionContext['type'];
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
}

/**
 * Interface for tracking actions across a single chat interaction session/turn.
 */
export interface ISessionActionObserver {
	/**
	 * Inspects a pending tool invocation before execution.
	 * Returns a pre-action explanation if the action is consequential/significant.
	 */
	recordBeforeToolInvocation(
		toolName: string,
		input: any,
		recalledMemories?: readonly ReeveMemoryItem[]
	): { action: ObservedAction };

	/**
	 * Records tool completion.
	 */
	recordAfterToolInvocation(
		actionId: string,
		result?: any,
		success?: boolean
	): void;

	/**
	 * Records a tool invocation directly (for backward compatibility / tests).
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

	/**
	 */
	getUserRequest(): string;
	isMeaningfulAction(action: ObservedAction): boolean;
}
