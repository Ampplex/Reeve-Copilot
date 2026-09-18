/*--------------------------------------------------------------------------------
 * Copyright (c) 2026 Ankesh Kumar.
 * Licensed under the MIT License. See License.txt in the project root for
 * license information.
 *--------------------------------------------------------------------------------*/

import { ActionCategory, ISessionActionObserver, ObservedAction } from '../common/reeveActionObserver';
import { ReeveMemoryItem } from '../common/reeveClient';

export class SessionActionObserver implements ISessionActionObserver {
	private readonly actions: ObservedAction[] = [];
	private counter = 0;

	constructor(public readonly sessionId: string, private readonly userRequest = '') { }

	recordBeforeToolInvocation(toolName: string, input: any, _recalledMemories: readonly ReeveMemoryItem[] = []): { action: ObservedAction } {
		const action = this.createObservedAction(toolName, input);
		this.actions.push(action);
		return { action };
	}

	recordAfterToolInvocation(actionId: string, result?: any, success = true): void {
		const action = this.actions.find(candidate => candidate.id === actionId);
		if (action) {
			action.executed = true;
			action.success = success;
			action.details = { ...action.details, result };
		}
	}

	recordToolInvocation(toolName: string, input: any, result?: any, success = true): ObservedAction | undefined {
		const { action } = this.recordBeforeToolInvocation(toolName, input);
		this.recordAfterToolInvocation(action.id, result, success);
		return action;
	}

	getActions(): readonly ObservedAction[] {
		return [...this.actions];
	}

	hasFileDeletion(): boolean {
		return this.actions.some(action => action.category === ActionCategory.FileDelete);
	}

	isMeaningfulAction(action: ObservedAction): boolean {
		return action.category === ActionCategory.FileEdit || action.category === ActionCategory.FileCreate || action.category === ActionCategory.FileDelete || action.category === ActionCategory.ShellCommand;
	}

	getUserRequest(): string {
		return this.userRequest;
	}

	private createObservedAction(toolName: string, input: any): ObservedAction {
		const normalizedName = String(toolName).toLowerCase().replace(/[_-]/g, '');
		const actionId = `act_${++this.counter}_${Date.now()}`;
		const filePath = input?.filePath || input?.targetFile || input?.path;
		const content = input?.replacementContent || input?.patch || input?.content || input?.contents || input?.code;

		if (normalizedName.includes('readfile') || normalizedName.includes('listdir') || normalizedName.includes('grepsearch') || normalizedName.includes('filesearch') || normalizedName.includes('semanticsearch') || normalizedName.includes('geterrors')) {
			return this.action(actionId, ActionCategory.CodebaseSearch, toolName, filePath, { input });
		}

		if (normalizedName.includes('createfile') || normalizedName.includes('createnewworkspace')) {
			return this.action(actionId, ActionCategory.FileCreate, toolName, filePath, { content, diff: input?.diff || input?.patch });
		}
		if (normalizedName.includes('deletefile') || normalizedName.includes('removefile')) {
			return this.action(actionId, ActionCategory.FileDelete, toolName, filePath, { input });
		}
		if (normalizedName.includes('insertedit') || normalizedName.includes('replacestring') || normalizedName.includes('applypatch') || normalizedName.includes('editnotebook') || normalizedName.includes('editfiles')) {
			return this.action(actionId, ActionCategory.FileEdit, toolName, filePath, { content, diff: input?.diff || input?.patch });
		}
		if (normalizedName.includes('terminal') || normalizedName.includes('runtask') || normalizedName.includes('createandruntask')) {
			const command = input?.command || input?.cmd || input?.text || '';
			return this.action(actionId, ActionCategory.ShellCommand, toolName, undefined, { command });
		}
		if (normalizedName.includes('test')) {
			return this.action(actionId, ActionCategory.TestRun, toolName, input?.testName || input?.file, { input });
		}
		return this.action(actionId, ActionCategory.Other, toolName, undefined, { input });
	}

	private action(id: string, category: ActionCategory, toolName: string, targetResource: string | undefined, details: Record<string, any>): ObservedAction {
		return { id, category, toolName, targetResource, details, timestamp: Date.now() };
	}
}
