/*--------------------------------------------------------------------------------
 * Copyright (c) 2026 Ankesh Kumar.
 * Licensed under the MIT License. See License.txt in the project root for
 * license information.
 *--------------------------------------------------------------------------------*/

import * as fs from 'node:fs';
import * as path from 'node:path';
import { describe, expect, it } from 'vitest';
import {
	ActionCategory,
	ChangeImpactLevel,
} from '../../common/reeveActionObserver';
import { ReeveMemoryItem } from '../../common/reeveClient';
import { HumanCenteredExplanationLayer, IExplanationStream } from '../humanCenteredExplanationLayer';
import { SessionActionObserver } from '../reeveActionObserver';

describe('Human-Centered Change Explanation Layer', () => {
	// TEST 1: Significant edit receives a pre-action explanation
	it('TEST 1: Significant edit receives a pre-action explanation before execution/approval', () => {
		const observer = new SessionActionObserver('session-1');
		const { action, preExplanation } = observer.recordBeforeToolInvocation('replace_string_in_file', {
			filePath: 'src/routes/auth.ts',
			replacementContent: `export function handleAuth() {\n  // consolidated token check\n  return validateToken();\n}\n\nexport function verifySession() {\n  return checkSession();\n}\n\nexport function checkPermissions() {\n  return true;\n}\n\n// additional lines\n// to exceed threshold\n// and mark significant\n// refactoring`,
		});

		expect(action.category).toBe(ActionCategory.FileEdit);
		expect(action.impactLevel).toBe(ChangeImpactLevel.Significant);
		expect(preExplanation).toBeDefined();
		expect(preExplanation).toContain('auth.ts');
	});

	// TEST 2: File deletion receives a pre-action explanation
	it('TEST 2: File deletion receives a pre-action explanation with reference evidence', () => {
		const observer = new SessionActionObserver('session-2');
		const { action, preExplanation } = observer.recordBeforeToolInvocation('run_in_terminal', {
			command: 'rm src/legacyAuth.ts',
		});

		expect(action.category).toBe(ActionCategory.FileDelete);
		expect(action.impactLevel).toBe(ChangeImpactLevel.Significant);
		expect(preExplanation).toBeDefined();
		expect(preExplanation).toContain('legacyAuth.ts');
		expect(preExplanation).toContain('couldn\'t find any active references');
	});

	// TEST 3: Destructive command receives natural, evidence-based pre-action explanation
	it('TEST 3: Destructive command rm -rf ./scratch/dist receives natural explanation without raw command or shell syntax', () => {
		const observer = new SessionActionObserver('session-3');
		const { action, preExplanation } = observer.recordBeforeToolInvocation('run_in_terminal', {
			command: 'rm -rf ./scratch/dist',
		});

		expect(action.isDestructive).toBe(true);
		expect(preExplanation).toBeDefined();
		expect(preExplanation).toContain('./scratch/dist');
		expect(preExplanation).toContain('generated build output');
		expect(preExplanation).toContain('rebuilding the project');

		// Explicitly assert it does NOT contain raw syntax, repetitive command, or AI banners
		expect(preExplanation).not.toContain('rm -rf');
		expect(preExplanation).not.toContain('removes or resets');
		expect(preExplanation).not.toContain('💡 Action Context');
	});

	// TEST 3b: Pre-action explanation streams naturally without AI prefix
	it('TEST 3b: onBeforeToolAction streams natural prose directly without AI dashboard prefix', () => {
		const layer = new HumanCenteredExplanationLayer();
		const streamed: string[] = [];
		const mockStream: IExplanationStream = { markdown: s => streamed.push(s) };

		layer.startSession('session-3b', mockStream);
		layer.onBeforeToolAction('run_in_terminal', {
			command: 'rm -rf ./scratch/dist && if [[ ! -e ./scratch/dist ]]; then printf "%s\\n" "./scratch/dist removed"; else exit 1; fi',
		}, 'session-3b');

		expect(streamed.length).toBe(1);
		expect(streamed[0]).toContain('./scratch/dist');
		expect(streamed[0]).toContain('generated build output');
		expect(streamed[0]).not.toContain('💡 Action Context');
		expect(streamed[0]).not.toContain('rm -rf');
		expect(streamed[0]).not.toContain('printf');
	});

	// TEST 4: Trivial edit does not generate unnecessary explanation
	it('TEST 4: Trivial edit does not generate unnecessary pre-action or post-action explanation', async () => {
		const layer = new HumanCenteredExplanationLayer();
		const streamParts: string[] = [];
		const mockStream: IExplanationStream = { markdown: s => streamParts.push(s) };

		layer.startSession('session-4', mockStream);
		const beforeResult = layer.onBeforeToolAction('replace_string_in_file', {
			filePath: 'src/user.ts',
			replacementContent: 'const userProfile = getUser();',
		}, 'session-4');

		expect(beforeResult?.preExplanation).toBeUndefined();

		const postResult = await layer.finalizeSession(
			'session-4',
			'Renamed getUser to getUserProfile.',
			mockStream
		);

		expect(postResult).toBeUndefined();
		expect(streamParts).toHaveLength(0);
	});

	// TEST 5: Existing natural agent explanation prevents duplicate explanation
	it('TEST 5: Existing natural agent explanation prevents duplicate post-change explanation', async () => {
		const layer = new HumanCenteredExplanationLayer();
		const streamParts: string[] = [];
		const mockStream: IExplanationStream = { markdown: s => streamParts.push(s) };

		const observer = layer.startSession('session-5', mockStream);
		observer.recordToolInvocation('replace_string_in_file', {
			filePath: 'src/routes/auth.ts',
			replacementContent: 'export function handleAuth() {\n  return validate();\n}',
		});
		observer.recordToolInvocation('replace_string_in_file', {
			filePath: 'src/routes/user.ts',
			replacementContent: 'export function handleUser() {\n  return validate();\n}',
		});
		observer.recordToolInvocation('replace_string_in_file', {
			filePath: 'src/middleware/token.ts',
			replacementContent: 'export function validate() {\n  // shared\n}',
		});

		// The agent already provided a thorough, natural explanation in its response
		const comprehensiveResponse =
			'I consolidated the duplicate token validation into AuthMiddleware and updated both routes that were doing it themselves. The API contract remains the same.';

		const result = await layer.finalizeSession('session-5', comprehensiveResponse, mockStream);
		expect(result).toBeUndefined();
		expect(streamParts).toHaveLength(0);
	});

	// TEST 6: Reeve memory can influence an explanation
	it('TEST 6: Reeve memory influences the explanation in natural language', () => {
		const observer = new SessionActionObserver('session-6');
		observer.recordToolInvocation('replace_string_in_file', {
			filePath: 'src/db.ts',
			replacementContent: 'export const pool = new Pool();',
		});

		const recalledMemories: ReeveMemoryItem[] = [
			{
				id: 'mem-1',
				content: 'Standardize database access on PostgreSQL connection pooling.',
				category: 'decision',
			},
		];

		const explanation = observer.generateExplanation('Done.', recalledMemories);
		expect(explanation).toBeDefined();
		expect(explanation!.relatedReeveDecisions.some(d => d.includes('PostgreSQL'))).toBe(true);
	});

	// TEST 7: Superseded memory is described as historical
	it('TEST 7: Superseded memory is explicitly described as historical and not current', () => {
		const observer = new SessionActionObserver('session-7');
		observer.recordToolInvocation('replace_string_in_file', {
			filePath: 'src/db.ts',
			replacementContent: 'export const client = new PostgresClient();',
		});

		const recalledMemories: ReeveMemoryItem[] = [
			{
				id: 'mem-old',
				content: 'Use MongoDB for customer records.',
				category: 'decision',
				supersededBy: 'mem-postgres',
			},
		];

		const explanation = observer.generateExplanation('Done.', recalledMemories);
		expect(explanation).toBeDefined();
		expect(
			explanation!.relatedReeveDecisions.some(
				d => d.includes('Historical note') && d.includes('superseded')
			)
		).toBe(true);
	});

	// TEST 8: Unable to determine intent results in explicit uncertainty
	it('TEST 8: Unable to determine full intent results in explicit grounded uncertainty', () => {
		const observer = new SessionActionObserver('session-8');
		observer.recordToolInvocation('run_in_terminal', {
			command: 'rm src/legacyAuth.ts',
		});

		const explanation = observer.generateExplanation('Done.');
		expect(explanation).toBeDefined();
		expect(explanation!.uncertainty).toBeDefined();
		expect(explanation!.uncertainty).toContain('haven\'t verified if external repositories');
	});

	// TEST 9: Humanized layer failure never breaks Copilot
	it('TEST 9: Humanized layer errors are handled fail-safe without throwing', async () => {
		const layer = new HumanCenteredExplanationLayer();
		layer.startSession('session-9');

		// Malformed inputs or broken stream
		const brokenStream: IExplanationStream = {
			markdown: () => {
				throw new Error('Stream rendering failed');
			},
		};

		// Should not throw
		await expect(
			layer.finalizeSession('session-9', 'Done.', brokenStream)
		).resolves.not.toThrow();
	});

	// TEST 10: Main Copilot identity prompt is NOT modified by the humanizer
	it('TEST 10: Main Copilot identity prompt in copilotIdentity.tsx does NOT contain humanizer rules', () => {
		const identityFilePath = path.resolve(__dirname, '../../../../extension/prompts/node/base/copilotIdentity.tsx');
		const fileContent = fs.readFileSync(identityFilePath, 'utf8');

		expect(fileContent).not.toContain('HumanCenteredExplanationRules');
		expect(fileContent).not.toContain('humanCenteredExplanationPrompt');
		expect(fileContent).toContain('CopilotIdentityRules');
	});

	// TEST 11: File edit does not fabricate 'duplicated logic'
	it('TEST 11: File edit does not fabricate duplicated logic without evidence', () => {
		const observer = new SessionActionObserver('session-11');
		const { preExplanation } = observer.recordBeforeToolInvocation('replace_string_in_file', {
			filePath: 'src/services/payment.ts',
			replacementContent: `export class PaymentService {\n  process() {\n    return true;\n  }\n  validate() {\n    return true;\n  }\n  refund() {\n    return false;\n  }\n  verify() {\n    return true;\n  }\n  cancel() {\n    return true;\n  }\n  track() {\n    return true;\n  }\n}`,
		});

		expect(preExplanation).toBeDefined();
		expect(preExplanation).toContain('payment.ts');
		// Must not make up claims about duplicated logic
		expect(preExplanation).not.toContain('duplicated logic');
		expect(preExplanation).not.toContain('duplicate check');
	});

	// TEST 12: Architectural change describes concrete contract without fabricating design philosophy
	it('TEST 12: Architectural change describes concrete contract without fabricating design philosophy', () => {
		const observer = new SessionActionObserver('session-12');
		const { preExplanation } = observer.recordBeforeToolInvocation('create_file', {
			filePath: 'src/repositories/userRepository.ts',
			content: `export interface UserRepository {\n  findById(id: string): Promise<User>;\n}\nexport class PostgresUserRepository implements UserRepository {\n  async findById(id: string) {\n    return db.query(id);\n  }\n}\n// 15 lines of logic\n// line 8\n// line 9\n// line 10\n// line 11\n// line 12\n// line 13\n// line 14\n// line 15`,
		});

		expect(preExplanation).toBeDefined();
		expect(preExplanation).toContain('UserRepository');
		expect(preExplanation).toContain('Repository');
		// Must not make up generic design slogans
		expect(preExplanation).not.toContain('to cleanly separate responsibilities');
		expect(preExplanation).not.toContain('from implementation details');
	});

	// TEST 13: Unknown deletion target honestly states lack of evidence
	it('TEST 13: Unknown deletion target honestly states lack of evidence', () => {
		const observer = new SessionActionObserver('session-13');
		const { preExplanation } = observer.recordBeforeToolInvocation('run_in_terminal', {
			command: 'rm -rf ./custom-data',
		});

		expect(preExplanation).toBeDefined();
		expect(preExplanation).toContain('./custom-data');
		expect(preExplanation).toContain('couldn\'t establish why it is safe to remove');
		// Must not claim it is build output
		expect(preExplanation).not.toContain('generated build output');
	});
});
