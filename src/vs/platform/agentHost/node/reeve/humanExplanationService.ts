/*--------------------------------------------------------------------------------
 * Copyright (c) 2026 Ankesh Kumar.
 * Licensed under the MIT License. See License.txt in the project root for
 * license information.
 *--------------------------------------------------------------------------------*/

import { ActionContext } from './reeveActionObserver.js';

export interface IHumanExplanationModel {
	explain(context: ActionContext): Promise<string | undefined>;
}

export class HumanExplanationService implements IHumanExplanationModel {
	async explain(context: ActionContext): Promise<string | undefined> {
		if (context.phase === 'before') {
			return this.explainBeforeAction(context);
		} else {
			return this.explainAfterAction(context);
		}
	}

	private explainBeforeAction(context: ActionContext): string | undefined {
		const target = context.target ? `\`${context.target}\`` : '';
		switch (context.type) {
			case 'edit':
				return `Editing ${target || 'file'} to implement requested changes`;
			case 'create':
				return `Creating new file ${target || ''}`;
			case 'delete':
				return `⚠️ Deleting file ${target || ''}`;
			case 'command': {
				const cmd = context.command ? `\`${context.command.trim()}\`` : 'shell command';
				if (/rm\s|rmdir|unlink/i.test(context.command || '')) {
					return `⚠️ Running destructive shell command: ${cmd}`;
				}
				return `Executing command: ${cmd}`;
			}
			case 'test':
				return `Running tests in ${target || 'workspace'}`;
			case 'read':
				return undefined; // quiet for routine reads
			default:
				return undefined;
		}
	}

	private explainAfterAction(context: ActionContext): string | undefined {
		const target = context.target ? `\`${context.target}\`` : '';
		switch (context.type) {
			case 'edit':
				return `Updated ${target}`;
			case 'create':
				return `Created ${target}`;
			case 'delete':
				return `Removed ${target}`;
			case 'command':
				return `Completed execution of \`${context.command || 'command'}\``;
			case 'test':
				return `Completed test run`;
			default:
				return undefined;
		}
	}
}
