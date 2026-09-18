/*--------------------------------------------------------------------------------
 * Copyright (c) 2026 Ankesh Kumar.
 * Licensed under the MIT License. See License.txt in the project root for
 * license information.
 *--------------------------------------------------------------------------------*/

import { ReeveActionEvent, ReeveActionType } from './reeveActionObserver';

/**
 * Translates Copilot-specific tool invocations into harness-agnostic ReeveActionEvents.
 */
export class CopilotActionAdapter {
	static toEvent(toolName: string, input: any, sessionId: string, actionId?: string): ReeveActionEvent {
		const normalizedName = String(toolName).toLowerCase().replace(/[_-]/g, '');
		const target = input?.filePath || input?.targetFile || input?.path;
		const content = input?.replacementContent || input?.patch || input?.content || input?.contents || input?.code;
		const diff = input?.diff || input?.patch;
		const command = input?.command || input?.cmd || input?.text || '';

		if (
			normalizedName.includes('readfile') ||
			normalizedName.includes('listdir') ||
			normalizedName.includes('grepsearch') ||
			normalizedName.includes('filesearch') ||
			normalizedName.includes('semanticsearch') ||
			normalizedName.includes('geterrors')
		) {
			return { harness: 'copilot', sessionId, actionId, toolName, type: 'read', target, input };
		}

		if (normalizedName.includes('createfile') || normalizedName.includes('createnewworkspace')) {
			return { harness: 'copilot', sessionId, actionId, toolName, type: 'create', target, content, diff, input };
		}

		if (normalizedName.includes('deletefile') || normalizedName.includes('removefile')) {
			return { harness: 'copilot', sessionId, actionId, toolName, type: 'delete', target, input, isDestructive: true };
		}

		if (
			normalizedName.includes('insertedit') ||
			normalizedName.includes('replacestring') ||
			normalizedName.includes('applypatch') ||
			normalizedName.includes('editnotebook') ||
			normalizedName.includes('editfiles')
		) {
			return { harness: 'copilot', sessionId, actionId, toolName, type: 'edit', target, content, diff, input };
		}

		if (
			normalizedName.includes('terminal') ||
			normalizedName.includes('runtask') ||
			normalizedName.includes('createandruntask')
		) {
			const isDestructive = /\b(rm\s|unlink\s|git\s+rm\b|rmdir\b)/i.test(command);
			return { harness: 'copilot', sessionId, actionId, toolName, type: 'command', command, input, isDestructive };
		}

		if (normalizedName.includes('test')) {
			return { harness: 'copilot', sessionId, actionId, toolName, type: 'test', target: input?.testName || input?.file, input };
		}

		return { harness: 'copilot', sessionId, actionId, toolName, type: 'other', input };
	}
}

/**
 * Translates Claude Code / Anthropic Agent SDK tool invocations into harness-agnostic ReeveActionEvents.
 */
export class ClaudeActionAdapter {
	static toEvent(toolName: string, input: any, sessionId: string, actionId?: string): ReeveActionEvent {
		const lowerName = String(toolName).toLowerCase().trim();
		const target = input?.path || input?.file_path || input?.target || input?.filePath;
		const command = input?.command || input?.cmd;
		const diff = input?.diff || input?.patch;
		const content = input?.content || input?.new_string || input?.text || input?.file_text;

		if (lowerName === 'bash' || lowerName === 'execute_command' || lowerName === 'terminal') {
			const cmdStr = String(command || '');
			const isDestructive = /\b(rm\s|unlink\s|git\s+rm\b|rmdir\b)/i.test(cmdStr);
			return { harness: 'claude', sessionId, actionId, toolName, type: 'command', command: cmdStr, input, isDestructive };
		}

		if (lowerName === 'edit' || lowerName === 'str_replace' || lowerName === 'strreplace' || lowerName === 'patch') {
			return { harness: 'claude', sessionId, actionId, toolName, type: 'edit', target, content, diff, input };
		}

		if (lowerName === 'write' || lowerName === 'create_file' || lowerName === 'write_to_file') {
			return { harness: 'claude', sessionId, actionId, toolName, type: 'create', target, content, input };
		}

		if (lowerName === 'read' || lowerName === 'view' || lowerName === 'view_file' || lowerName === 'glob' || lowerName === 'grep' || lowerName === 'ls') {
			return { harness: 'claude', sessionId, actionId, toolName, type: 'read', target, input };
		}

		return { harness: 'claude', sessionId, actionId, toolName, type: 'other', input };
	}
}
