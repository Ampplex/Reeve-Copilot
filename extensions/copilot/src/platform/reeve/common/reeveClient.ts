/*--------------------------------------------------------------------------------
 * Copyright (c) 2026 Ankesh Kumar.
 * Licensed under the MIT License. See License.txt in the project root for
 * license information.
 *--------------------------------------------------------------------------------*/

import { createServiceIdentifier } from '../../../util/common/services';
import { CancellationToken } from '../../../util/vs/base/common/cancellation';

export const IReeveClient = createServiceIdentifier<IReeveClient>('IReeveClient');

/**
 * A temporal memory record or resolved fact from Reeve's knowledge graph.
 */
export interface ReeveMemoryItem {
	readonly id: string;
	readonly content: string;
	readonly category?: 'decision' | 'constraint' | 'architecture' | 'history' | string;
	readonly timestamp?: string;
	readonly validFrom?: string;
	readonly validTo?: string;
	readonly supersededBy?: string;
	readonly tags?: readonly string[];
	readonly score?: number;
}

export interface ReeveSearchParams {
	readonly query: string;
	readonly namespace?: string;
	readonly speaker?: string;
	readonly category?: string;
	readonly limit?: number;
}

export interface ReeveSearchResult {
	readonly success: boolean;
	readonly items: readonly ReeveMemoryItem[];
	readonly namespace: string;
	readonly answer?: string;
	readonly error?: string;
}

export interface ReeveStoreParams {
	readonly fact: string;
	readonly namespace?: string;
	readonly speaker?: string;
	readonly category?: string;
}

export interface ReeveStoreResult {
	readonly success: boolean;
	readonly id?: string;
	readonly error?: string;
}

export interface IReeveClient {
	readonly _serviceBrand: undefined;

	/**
	 * Returns the resolved workspace namespace used to isolate memory per project.
	 */
	getNamespace(): string;

	/**
	 * Whether the Reeve integration is enabled via configuration.
	 */
	isEnabled(): boolean;

	/**
	 * Query Reeve temporal project memory.
	 * Returns structurally-resolved facts and context from the temporal knowledge graph.
	 * Fail-safe: Always returns a result object (never throws on network error or timeout).
	 */
	queryMemory(params: ReeveSearchParams, token?: CancellationToken): Promise<ReeveSearchResult>;

	/**
	 * Retrieve temporally-aware context for a specific entity or topic.
	 */
	retrieveContext(topicOrEntity: string, namespace?: string, token?: CancellationToken): Promise<ReeveSearchResult>;

	/**
	 * Store a durable new fact or architectural decision into the graph.
	 */
	storeMemory?(params: ReeveStoreParams, token?: CancellationToken): Promise<ReeveStoreResult>;

	/**
	 * Encapsulated high-level helpers for chat participants:
	 */
	preparePromptWithMemory?(
		request: any,
		stream: any,
		token?: CancellationToken
	): Promise<{ request: any; hasMemory: boolean; namespace: string }>;

	recordInteraction?(
		userPrompt: string,
		references?: readonly any[]
	): Promise<void>;

	recordAgentResponse?(
		agentResponse: string
	): Promise<void>;

	renderMemoryCitation?(
		stream: any,
		namespace: string
	): void;

	/**
	 * Action Observer & Human-Centered Explanation Layer:
	 */
	startActionObservation?(sessionId: string, stream?: any): any;

	onBeforeToolAction?(
		toolName: string,
		input: any,
		sessionId?: string
	): { action: any; preExplanation?: string } | undefined;

	onAfterToolAction?(
		actionId: string,
		result?: any,
		success?: boolean,
		sessionId?: string
	): void;

	recordToolAction?(toolName: string, input: any, result?: any, success?: boolean, sessionId?: string): void;

	finalizeActionObservation?(
		sessionId: string,
		agentResponseText: string,
		stream?: any
	): Promise<any>;
}
