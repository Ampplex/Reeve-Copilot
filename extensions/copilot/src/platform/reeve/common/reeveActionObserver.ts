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

/**
 * The assessed impact level of an action or group of changes.
 */
export enum ChangeImpactLevel {
	Trivial = 'trivial',
	Normal = 'normal',
	Significant = 'significant',
	Architectural = 'architectural',
}

/**
 * Information captured from a single agent tool invocation.
 */
export interface ObservedAction {
	readonly id: string;
	readonly category: ActionCategory;
	readonly toolName: string;
	readonly targetResource?: string; // Target file or command
	readonly details?: Record<string, any>;
	readonly timestamp: number;
	readonly impactLevel: ChangeImpactLevel;
	readonly linesChanged?: number;
	readonly hasComplexRegex?: boolean;
	readonly detectedRegex?: string;
	readonly isDestructive?: boolean;
	readonly architecturalBoundary?: string;
	readonly structuralElements?: {
		readonly interfaces: readonly string[];
		readonly classes: readonly string[];
		readonly functions: readonly string[];
	};
	executed?: boolean;
	success?: boolean;
}

/**
 * The resulting human-centered explanation produced for the developer.
 */
export interface ActionExplanation {
	readonly impactLevel: ChangeImpactLevel;
	readonly summary: string;
	readonly details: readonly string[];
	readonly relatedReeveDecisions: readonly string[];
	readonly uncertainty?: string;
	readonly isArchitectureChange?: boolean;
	readonly beforeExplanation?: string;
	readonly afterExplanation?: string;
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
	): { action: ObservedAction; preExplanation?: string };

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

	/**
	 * Evaluates the cumulative impact level across all observed actions.
	 */
	getOverallImpact(): ChangeImpactLevel;

	/**
	 * Checks if the agent's generated response adequately explains significant changes.
	 */
	isExplanationAdequate(agentResponseText: string): boolean;

	/**
	 * Produces a human-centered explanation if needed, incorporating relevant Reeve memory.
	 */
	generateExplanation(
		agentResponseText: string,
		recalledMemories?: readonly ReeveMemoryItem[]
	): ActionExplanation | undefined;
}
