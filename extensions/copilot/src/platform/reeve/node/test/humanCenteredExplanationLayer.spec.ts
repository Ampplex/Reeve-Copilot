/*--------------------------------------------------------------------------------
 * Copyright (c) 2026 Ankesh Kumar.
 * Licensed under the MIT License. See License.txt in the project root for
 * license information.
 *--------------------------------------------------------------------------------*/

import { describe, expect, it, vi } from 'vitest';
import { ActionCategory } from '../../common/reeveActionObserver';
import { HumanCenteredExplanationLayer, IExplanationStream } from '../humanCenteredExplanationLayer';
import { HUMAN_EXPLANATION_PROMPT, IHumanExplanationModel } from '../humanExplanationService';
import { SessionActionObserver } from '../reeveActionObserver';

class FakeExplanationModel implements IHumanExplanationModel {
	readonly contexts: any[] = [];
	constructor(private readonly response = 'The requested change updates the authentication flow.') { }

	async explain(context: any): Promise<string | undefined> {
		this.contexts.push(context);
		return this.response;
	}
}

describe('Human explanation architecture', () => {
	it('detects whether a session recorded a file deletion', () => {
		const observer = new SessionActionObserver('session-delete');

		expect(observer.hasFileDeletion()).toBe(false);
		observer.recordToolInvocation('delete_file', { filePath: 'src/legacyAuth.ts' });

		expect(observer.hasFileDeletion()).toBe(true);
	});

	it('observes actions without interpreting their engineering meaning', () => {
		const observer = new SessionActionObserver('session-1', 'Add authentication caching.');
		const { action } = observer.recordBeforeToolInvocation('replace_string_in_file', {
			filePath: 'src/auth/cache.ts',
			diff: '+ export const cache = new Map();',
		});

		expect(action.category).toBe(ActionCategory.FileEdit);
		expect(action.targetResource).toBe('src/auth/cache.ts');
		expect(action.details?.diff).toContain('Map');
		expect(action).not.toHaveProperty('detectedRegex');
		expect(action).not.toHaveProperty('architecturalBoundary');
	});

	it('ignores informational actions', async () => {
		const model = new FakeExplanationModel();
		const layer = new HumanCenteredExplanationLayer(model);
		layer.startSession('session-2');

		const result = await layer.onBeforeToolAction('read_file', { filePath: 'src/auth/cache.ts' }, 'session-2');

		expect(result?.action.category).toBe(ActionCategory.CodebaseSearch);
		expect(model.contexts).toHaveLength(0);
	});

	it('sends structured action evidence and streams the model explanation before execution', async () => {
		const model = new FakeExplanationModel('I am adding the cache around the existing authentication flow.');
		const streamed: string[] = [];
		const stream: IExplanationStream = { markdown: value => streamed.push(value) };
		const layer = new HumanCenteredExplanationLayer(model);
		layer.startSession('session-3', stream, 'Add authentication caching.');

		const result = await layer.onBeforeToolAction('replace_string_in_file', {
			filePath: 'src/auth/cache.ts',
			diff: '+ export const cache = new Map();',
		}, 'session-3', [{ id: 'memory-1', content: 'Authentication state is centralized in AuthService.', category: 'architecture' }]);

		expect(result?.preExplanation).toContain('existing authentication flow');
		expect(model.contexts[0]).toMatchObject({
			type: 'edit',
			target: 'src/auth/cache.ts',
			diff: '+ export const cache = new Map();',
			userRequest: 'Add authentication caching.',
			reeveMemory: 'Authentication state is centralized in AuthService.',
		});
		expect(streamed[0]).toContain('existing authentication flow');
	});

	it('includes the result and marks superseded Reeve memory as historical', async () => {
		const model = new FakeExplanationModel('The old authentication file was removed; the active flow is unchanged.');
		const layer = new HumanCenteredExplanationLayer(model);
		layer.startSession('session-4');
		const before = await layer.onBeforeToolAction('run_in_terminal', { command: 'rm src/legacyAuth.ts' }, 'session-4', [{
			id: 'old', content: 'Use the legacy authentication file.', category: 'decision', supersededBy: 'new',
		}]);
		layer.onAfterToolAction(before!.action.id, 'removed successfully', true, 'session-4');

		await layer.finalizeSession('session-4', 'Done.');
		expect(model.contexts[1]).toMatchObject({
			type: 'command',
			command: 'rm src/legacyAuth.ts',
			result: 'removed successfully',
			reeveMemory: 'Historical (superseded): Use the legacy authentication file.',
		});
	});

	it('gives the model the existing agent explanation to avoid duplication', async () => {
		const model = new FakeExplanationModel();
		const layer = new HumanCenteredExplanationLayer(model);
		layer.startSession('session-5');
		await layer.onBeforeToolAction('replace_string_in_file', { filePath: 'src/auth.ts', diff: 'change' }, 'session-5');
		const result = await layer.finalizeSession('session-5', 'I updated the authentication cache and preserved the existing authentication flow.');

		expect(result).toBeDefined();
		expect(model.contexts[1].agentResponse).toContain('preserved the existing authentication flow');
		expect(model.contexts[1].priorExplanation).toBeDefined();
	});

	it('keeps the prompt focused on evidence and non-fabrication', () => {
		expect(HUMAN_EXPLANATION_PROMPT).toContain('Use only the supplied evidence');
		expect(HUMAN_EXPLANATION_PROMPT).toContain('Do not invent reasoning');
		expect(HUMAN_EXPLANATION_PROMPT).not.toContain('Action Context');
	});

	it('sends all meaningful actions to the final explanation model', async () => {
		const model = new FakeExplanationModel();
		const layer = new HumanCenteredExplanationLayer(model);
		layer.startSession('session-7', undefined, 'Update authentication.');
		await layer.onBeforeToolAction('replace_string_in_file', { filePath: 'src/auth.ts', diff: 'auth change' }, 'session-7');
		await layer.onBeforeToolAction('create_file', { filePath: 'src/auth/cache.ts', content: 'cache' }, 'session-7');
		await layer.finalizeSession('session-7', 'Completed both authentication changes.');

		const finalContext = model.contexts[2];
		expect(finalContext.phase).toBe('after');
		expect(finalContext.actions).toHaveLength(2);
		expect(finalContext.actions.map((action: { target?: string }) => action.target)).toEqual([
			'src/auth.ts',
			'src/auth/cache.ts',
		]);
	});

	it('fails safe when the explanation model fails', async () => {
		const model: IHumanExplanationModel = { explain: vi.fn(async () => undefined) };
		const layer = new HumanCenteredExplanationLayer(model);
		layer.startSession('session-6');
		const result = await layer.onBeforeToolAction('create_file', { filePath: 'src/new.ts', content: 'export {}' }, 'session-6');
		expect(result?.preExplanation).toBeUndefined();
	});
});
