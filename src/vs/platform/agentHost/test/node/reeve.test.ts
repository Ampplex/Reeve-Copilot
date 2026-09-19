/*--------------------------------------------------------------------------------
 * Copyright (c) 2026 Ankesh Kumar.
 * Licensed under the MIT License. See License.txt in the project root for
 * license information.
 *--------------------------------------------------------------------------------*/

import assert from 'assert';
import { ensureNoDisposablesAreLeakedInTestSuite } from '../../../../base/test/common/utils.js';
import { ActionCategory, SessionActionObserver } from '../../node/reeve/reeveActionObserver.js';
import { ClaudeActionAdapter, CopilotActionAdapter } from '../../node/reeve/reeveAdapters.js';
import { HumanCenteredExplanationLayer } from '../../node/reeve/humanCenteredExplanationLayer.js';
import { ReeveClient, ReeveMemoryItem } from '../../node/reeve/reeveClient.js';

suite('Agent Host — Reeve Layer (Provider Agnostic)', () => {
	ensureNoDisposablesAreLeakedInTestSuite();

	test('ClaudeActionAdapter translates Claude tool calls to ReeveActionEvents accurately', () => {
		// Edit
		const editEvent = ClaudeActionAdapter.toEvent('Edit', {
			file_path: 'src/auth/authService.ts',
			old_string: 'const token = 1;',
			new_string: 'const token = 2;',
		}, 'sess_1', 'toolu_1');

		assert.strictEqual(editEvent.harness, 'claude');
		assert.strictEqual(editEvent.sessionId, 'sess_1');
		assert.strictEqual(editEvent.actionId, 'toolu_1');
		assert.strictEqual(editEvent.type, 'edit');
		assert.strictEqual(editEvent.target, 'src/auth/authService.ts');
		assert.strictEqual(editEvent.content, 'const token = 2;');

		// Write
		const writeEvent = ClaudeActionAdapter.toEvent('Write', {
			file_path: 'src/utils/logger.ts',
			content: 'export const log = console.log;',
		}, 'sess_1', 'toolu_2');

		assert.strictEqual(writeEvent.type, 'create');
		assert.strictEqual(writeEvent.target, 'src/utils/logger.ts');
		assert.strictEqual(writeEvent.content, 'export const log = console.log;');

		// Bash (non-destructive)
		const bashEvent = ClaudeActionAdapter.toEvent('Bash', {
			command: 'npm test',
		}, 'sess_1', 'toolu_3');

		assert.strictEqual(bashEvent.type, 'command');
		assert.strictEqual(bashEvent.command, 'npm test');
		assert.strictEqual(bashEvent.isDestructive, false);

		// Bash (destructive)
		const destructiveBash = ClaudeActionAdapter.toEvent('Bash', {
			command: 'rm -rf ./dist',
		}, 'sess_1', 'toolu_4');

		assert.strictEqual(destructiveBash.type, 'command');
		assert.strictEqual(destructiveBash.isDestructive, true);

		// Read
		const readEvent = ClaudeActionAdapter.toEvent('Read', {
			file_path: 'package.json',
		}, 'sess_1', 'toolu_5');

		assert.strictEqual(readEvent.type, 'read');
		assert.strictEqual(readEvent.target, 'package.json');
	});

	test('CopilotActionAdapter translates Copilot tool calls to ReeveActionEvents accurately', () => {
		const editEvent = CopilotActionAdapter.toEvent('applyPatch', {
			filePath: 'src/index.ts',
			patch: '@@ -1 +1 @@',
		}, 'sess_2', 'copilot_tool_1');

		assert.strictEqual(editEvent.harness, 'copilot');
		assert.strictEqual(editEvent.type, 'edit');
		assert.strictEqual(editEvent.target, 'src/index.ts');

		const termEvent = CopilotActionAdapter.toEvent('runInTerminal', {
			command: 'git status',
		}, 'sess_2', 'copilot_tool_2');

		assert.strictEqual(termEvent.type, 'command');
		assert.strictEqual(termEvent.command, 'git status');
		assert.strictEqual(termEvent.isDestructive, false);
	});

	test('SessionActionObserver tracks actions lifecycle and detects meaningful/destructive actions', () => {
		const observer = new SessionActionObserver('sess_1', 'Refactor auth service');

		const editEvent = ClaudeActionAdapter.toEvent('Edit', {
			file_path: 'src/auth/authService.ts',
			new_string: 'export class AuthService {}',
		}, 'sess_1', 'toolu_1');

		const { action } = observer.recordBeforeAction(editEvent);
		assert.strictEqual(action.category, ActionCategory.FileEdit);
		assert.strictEqual(observer.isMeaningfulAction(action), true);
		assert.strictEqual(action.executed, undefined);

		observer.recordAfterAction({
			...editEvent,
			result: 'File updated successfully',
			success: true,
		});

		const actions = observer.getActions();
		assert.strictEqual(actions.length, 1);
		assert.strictEqual(actions[0].executed, true);
		assert.strictEqual(actions[0].success, true);
		assert.strictEqual(actions[0].details?.result, 'File updated successfully');
	});

	test('HumanCenteredExplanationLayer generates pre-action explanation and finalizes session', async () => {
		const layer = new HumanCenteredExplanationLayer();
		layer.startSession('sess_1', undefined, 'Update configuration');

		const editEvent = ClaudeActionAdapter.toEvent('Edit', {
			file_path: 'src/config.ts',
			new_string: 'port: 8080',
		}, 'sess_1', 'toolu_1');

		const result = await layer.onBeforeAction(editEvent, [{
			id: 'mem_1',
			content: 'Server must default to port 8080',
		}]);

		assert.ok(result);
		assert.ok(result.preExplanation);
		assert.ok(result.preExplanation.includes('src/config.ts'));

		layer.onAfterAction({
			...editEvent,
			result: 'ok',
			success: true,
		});

		const finalExplanation = await layer.finalizeSession('sess_1', 'Done updating config.');
		assert.ok(finalExplanation);
		assert.ok(finalExplanation.summary);
	});

	test('ReeveClient.formatMemoriesForContext formats memory items with historical badges', () => {
		const memories: ReeveMemoryItem[] = [
			{ id: '1', content: 'Centralized state is in StoreManager.' },
			{ id: '2', content: 'Legacy Auth is deprecated.', isHistorical: true },
		];

		const formatted = ReeveClient.formatMemoriesForContext(memories);
		assert.ok(formatted.includes('Centralized state is in StoreManager.'));
		assert.ok(formatted.includes('*(Historical / Superseded)* Legacy Auth is deprecated.'));
	});
});
