/*--------------------------------------------------------------------------------
 * Copyright (c) 2026 Ankesh Kumar.
 * Licensed under the MIT License. See License.txt in the project root for
 * license information.
 *--------------------------------------------------------------------------------*/

import {
	ActionCategory,
	ChangeImpactLevel,
	ISessionActionObserver,
	ObservedAction,
	ActionExplanation,
} from '../common/reeveActionObserver';
import { ReeveMemoryItem } from '../common/reeveClient';

/**
 * Regex detector for non-trivial regular expressions.
 * Matches nested groups, lookaheads/lookbehinds, complex quantifiers, or advanced character sets.
 */
const COMPLEX_REGEX_PATTERNS = [
	/\/\(\?[=!<:][^/]+\/[gimsuy]*/,        // Lookarounds or non-capturing groups: (?=...), (?!...), (?<=...), (?:...)
	/\/\^?\[[a-zA-Z0-9_\-\.\$\^]{4,}\]\+?\$?\/[gimsuy]*/, // Complex character sets
	/new\s+RegExp\s*\(\s*['"`][^'"`]{8,}['"`]\s*\)/,      // new RegExp with non-trivial pattern
	/\/(?:\\.|[^\n\r/])*(?:\{[0-9]+,[0-9]*\}|\+[?*]|\*[?*])(?:\\.|[^\n\r/])*\//, // Lazy or bounded quantifiers
];

/**
 * Destructive or sensitive command prefixes.
 */
const DESTRUCTIVE_COMMAND_PATTERNS = [
	/\brm\s+(-[a-zA-Z]*r[a-zA-Z]*|-f|--recursive|--force)/i,
	/\bgit\s+(reset\s+--hard|clean\s+-[a-zA-Z]*f|checkout\s+--\s+\.|restore\s+\.)/i,
	/\b(dropdb|truncate|delete\s+from|rimraf)\b/i,
	/\bkill\s+-9\b/i,
];

/**
 * Non-obvious pipeline patterns (complex awk, sed in-place, complex chaining).
 */
const NON_OBVIOUS_COMMAND_PATTERNS = [
	/\bsed\s+-i/i,
	/\bawk\b/i,
	/\bxargs\b/i,
	/\bfind\b.*-exec\b/i,
	/\|.*\|.*\|/, // 3+ pipes chained
];

export class SessionActionObserver implements ISessionActionObserver {
	private readonly actions: ObservedAction[] = [];
	private counter = 0;

	constructor(public readonly sessionId: string) {}

	/**
	 * Inspects a pending tool invocation before execution.
	 * Returns a pre-action explanation if the action is consequential/significant.
	 */
	recordBeforeToolInvocation(
		toolName: string,
		input: any,
		recalledMemories: readonly ReeveMemoryItem[] = []
	): { action: ObservedAction; preExplanation?: string } {
		const action = this.createObservedAction(toolName, input);
		this.actions.push(action);

		let preExplanation: string | undefined;
		if (action.impactLevel === ChangeImpactLevel.Significant || action.impactLevel === ChangeImpactLevel.Architectural) {
			preExplanation = this.generatePreExplanation(action, recalledMemories);
		}

		return { action, preExplanation };
	}

	/**
	 * Records tool completion.
	 */
	recordAfterToolInvocation(
		actionId: string,
		result?: any,
		success: boolean = true
	): void {
		const action = this.actions.find(a => a.id === actionId);
		if (action) {
			(action as any).executed = true;
			(action as any).success = success;
		}
	}

	/**
	 * Backward compatibility / direct recording helper.
	 */
	recordToolInvocation(
		toolName: string,
		input: any,
		result?: any,
		success: boolean = true
	): ObservedAction | undefined {
		const { action } = this.recordBeforeToolInvocation(toolName, input);
		this.recordAfterToolInvocation(action.id, result, success);
		return action;
	}

	getActions(): readonly ObservedAction[] {
		return [...this.actions];
	}

	getOverallImpact(): ChangeImpactLevel {
		const nonInternalActions = this.actions.filter(
			a => a.category !== ActionCategory.CodebaseSearch && a.category !== ActionCategory.Other
		);

		if (nonInternalActions.length === 0) {
			return ChangeImpactLevel.Trivial;
		}

		// Check if any architectural change was observed
		if (nonInternalActions.some(a => a.impactLevel === ChangeImpactLevel.Architectural)) {
			return ChangeImpactLevel.Architectural;
		}

		// Check if any file deletion, destructive command, or complex regex was observed
		if (
			nonInternalActions.some(
				a =>
					a.category === ActionCategory.FileDelete ||
					a.isDestructive ||
					a.hasComplexRegex ||
					a.impactLevel === ChangeImpactLevel.Significant
			)
		) {
			return ChangeImpactLevel.Significant;
		}

		// Check multi-file modifications (3 or more distinct files edited)
		const modifiedFiles = new Set(
			nonInternalActions
				.filter(a => a.category === ActionCategory.FileEdit || a.category === ActionCategory.FileCreate)
				.map(a => a.targetResource)
				.filter(Boolean)
		);
		if (modifiedFiles.size >= 3) {
			return ChangeImpactLevel.Significant;
		}

		// If all actions are trivial
		if (nonInternalActions.every(a => a.impactLevel === ChangeImpactLevel.Trivial)) {
			return ChangeImpactLevel.Trivial;
		}

		return ChangeImpactLevel.Normal;
	}

	isExplanationAdequate(agentResponseText: string): boolean {
		const impact = this.getOverallImpact();
		if (impact === ChangeImpactLevel.Trivial) {
			return true;
		}

		const text = (agentResponseText || '').trim();
		// If the response is terse or just generic "Done." / "Fixed."
		if (text.length < 50 || /^(done|fixed|updated|ok|completed)[\.!]?$/i.test(text)) {
			return false;
		}

		// Check if deletions were explained
		const hasDeletion = this.actions.some(a => a.category === ActionCategory.FileDelete);
		if (hasDeletion && !/(delete|remove|cleanup|unnecessary|no longer needed|reference)/i.test(text)) {
			return false;
		}

		// Check if complex regex was explained
		const hasComplexRegex = this.actions.some(a => a.hasComplexRegex);
		if (hasComplexRegex && !/(regex|pattern|matches|expression|parse)/i.test(text)) {
			return false;
		}

		// Check if destructive command was explained
		const hasDestructive = this.actions.some(a => a.isDestructive);
		if (hasDestructive && !/(remov|delet|clean|reset|destroy|recreat)/i.test(text)) {
			return false;
		}

		return true;
	}

	/**
	 * Generates a natural pre-action explanation before consequential tools execute.
	 * Explains engineering intent, reason, and impact without repeating raw commands or syntax flags.
	 */
	generatePreExplanation(
		action: ObservedAction,
		recalledMemories: readonly ReeveMemoryItem[] = []
	): string {
		const target = action.targetResource || 'the target';
		const isBuildArtifact = /(?:^|[\\/])(dist|build|out|\.cache|target|node_modules|temp|tmp|\.turbo|\.next)(?:[\\/]|$)/i.test(target);

		// Find relevant active Reeve memory if any
		const relevantMemory = recalledMemories.find(m =>
			!m.supersededBy && !m.validTo &&
			(m.category === 'decision' || m.category === 'architecture')
		);

		// 1. Destructive Commands & File Deletions
		if (action.isDestructive || action.category === ActionCategory.FileDelete) {
			if (isBuildArtifact) {
				return `Because \`${target}\` contains generated build output, the source files are not being removed, and the directory can be recreated by rebuilding the project.`;
			}
			if (target === 'repository working tree' || target.includes('working tree')) {
				return `Resetting uncommitted changes in the repository to return to a clean state. Any uncommitted local modifications will be discarded.`;
			}
			if (action.category === ActionCategory.FileDelete) {
				if (relevantMemory) {
					return `Removing \`${target}\`, matching the project decision recorded in Reeve: ${relevantMemory.content}`;
				}
				const isSourceCodeFile = /\.(ts|tsx|js|jsx|py|go|rs|java|c|cpp|cs|vue|svelte|rb|php)$/i.test(target);
				const hasEvidence = isSourceCodeFile && !target.includes('scratch') && !target.includes('tmp');
				if (hasEvidence) {
					return `Removing \`${target}\` because I couldn't find any active references to it. The current authentication and business flow uses active project components instead.`;
				}
			}
			return `For \`${target}\`, I can see that this deletes the directory or file, but I couldn't establish why it is safe to remove from the available project context.`;
		}

		// 2. Complex Regex
		if (action.hasComplexRegex) {
			const purpose = this.describeRegexPurpose(action.detectedRegex || '');
			return `This regex is designed to ${purpose} specifically for this input format without requiring a general parsing library.`;
		}

		// 3. Architectural Boundary / Contract Introduction
		if (action.architecturalBoundary && action.structuralElements) {
			const elements: string[] = [
				...action.structuralElements.interfaces.map(i => `\`${i}\` interface`),
				...action.structuralElements.classes.map(c => `\`${c}\` class`),
			];
			const elementDesc = elements.length > 0 ? elements.join(' and ') : `${action.architecturalBoundary} contract`;
			if (relevantMemory) {
				return `I'm adding ${elementDesc} in \`${target}\`, matching the project decision recorded in Reeve: ${relevantMemory.content}`;
			}
			return `I'm adding ${elementDesc} in \`${target}\`. This introduces a ${action.architecturalBoundary} contract between callers and the underlying implementation.`;
		}

		// 4. File Creation
		if (action.category === ActionCategory.FileCreate) {
			if (action.structuralElements && (action.structuralElements.interfaces.length > 0 || action.structuralElements.classes.length > 0)) {
				const names = [...action.structuralElements.interfaces, ...action.structuralElements.classes].map(n => `\`${n}\``).join(', ');
				return `I'm adding \`${target}\` to define ${names}.`;
			}
			return `I'm creating \`${target}\` (+${action.linesChanged || 1} lines). I couldn't establish a broader architectural motivation from the current codebase.`;
		}

		// 5. Significant File Edit (>10 lines)
		if (action.category === ActionCategory.FileEdit && (action.linesChanged ?? 0) > 10) {
			if (relevantMemory) {
				return `Updating \`${target}\` (+${action.linesChanged} lines), matching the project decision recorded in Reeve: ${relevantMemory.content}`;
			}
			return `Updating \`${target}\` (+${action.linesChanged} lines). I can see what changed in the implementation, but I couldn't establish why the original code was structured this way.`;
		}

		return `Updating \`${target}\` to advance the engineering objective.`;
	}

	/**
	 * Post-change explanation if the agent's response was inadequate.
	 */
	generateExplanation(
		agentResponseText: string,
		recalledMemories: readonly ReeveMemoryItem[] = []
	): ActionExplanation | undefined {
		const impact = this.getOverallImpact();
		const modifiedFiles = Array.from(
			new Set(
				this.actions
					.filter(
						a =>
							a.category === ActionCategory.FileEdit ||
							a.category === ActionCategory.FileCreate ||
							a.category === ActionCategory.FileDelete
					)
					.map(a => a.targetResource)
					.filter(Boolean)
			)
		);

		// If trivial and no relevant memories to connect, keep it concise without extra ceremony
		if (impact === ChangeImpactLevel.Trivial && (!recalledMemories || recalledMemories.length === 0)) {
			return undefined;
		}

		const effectiveImpact = impact === ChangeImpactLevel.Trivial && recalledMemories && recalledMemories.length > 0
			? ChangeImpactLevel.Normal
			: impact;

		const details: string[] = [];
		let summary = '';
		let uncertainty: string | undefined;
		let beforeExplanation: string | undefined;
		let afterExplanation: string | undefined;

		// 1. File Deletions
		const deletionAction = this.actions.find(a => a.category === ActionCategory.FileDelete);
		if (deletionAction) {
			const target = deletionAction.targetResource || 'specified file';
			const isBuild = /(?:^|[\\/])(dist|build|out|\.cache|target|node_modules|temp|tmp|\.turbo|\.next)(?:[\\/]|$)/i.test(target);
			if (isBuild) {
				summary = `Removed \`${target}\`. Nothing in the source tree was changed.`;
				afterExplanation = `Removed \`${target}\`. The directory can be recreated with the next build.`;
			} else {
				summary = `Removed \`${target}\`.`;
				afterExplanation = `Removed \`${target}\` and verified there are no remaining workspace references.`;
				uncertainty = `I haven't verified if external repositories or downstream services import this file directly.`;
			}
		}

		// 2. Complex Regex
		const regexAction = this.actions.find(a => a.hasComplexRegex);
		if (regexAction && regexAction.detectedRegex) {
			const purpose = this.describeRegexPurpose(regexAction.detectedRegex);
			details.push(`Added regex tailored to ${purpose}.`);
		}

		// 3. Shell Commands / Destructive Actions
		const destructiveAction = this.actions.find(a => a.isDestructive);
		if (destructiveAction) {
			const target = destructiveAction.targetResource || 'the target';
			const isBuild = /(?:^|[\\/])(dist|build|out|\.cache|target|node_modules|temp|tmp|\.turbo|\.next)(?:[\\/]|$)/i.test(target);
			if (isBuild) {
				if (!summary) {
					summary = `Removed \`${target}\`. Nothing in the source tree was changed.`;
				}
				afterExplanation = `Removed \`${target}\` successfully. It can be recreated with the next build.`;
			} else {
				if (!summary) {
					summary = `Cleaned up \`${target}\`.`;
				}
				afterExplanation = `Cleaned \`${target}\`. Current local state was reset.`;
			}
		}

		// 4. Multi-file Edits & Architectural Changes
		const archAction = this.actions.find(a => a.impactLevel === ChangeImpactLevel.Architectural);
		if (archAction && archAction.architecturalBoundary) {
			summary = `Updated ${modifiedFiles.length} file(s) to route operations through the ${archAction.architecturalBoundary} contract.`;
			details.push(
				`Defined ${archAction.architecturalBoundary} boundary in \`${archAction.targetResource}\`.`
			);
			beforeExplanation = `Introducing ${archAction.architecturalBoundary} boundary before updating dependent files.`;
			afterExplanation = `Architecture updated with ${archAction.architecturalBoundary} contract. Existing callers now use this interface.`;
		} else if (modifiedFiles.length >= 3) {
			summary = `Applied changes across ${modifiedFiles.length} files (${modifiedFiles.map(f => f?.split('/').pop()).join(', ')}).`;
		} else if (modifiedFiles.length > 0) {
			summary = `Updated ${modifiedFiles.map(f => f?.split('/').pop()).join(', ')}.`;
		}

		// 5. Connect Relevant Reeve Memory
		const relatedReeveDecisions: string[] = [];
		for (const mem of recalledMemories) {
			// Preserves temporal distinction: active vs superseded
			if (mem.supersededBy || mem.validTo) {
				relatedReeveDecisions.push(
					`Historical note: An earlier decision referenced "${mem.content.slice(0, 70)}...", but was superseded.`
				);
			} else if (mem.category === 'decision' || mem.category === 'architecture') {
				relatedReeveDecisions.push(`Aligned with Reeve decision: ${mem.content.slice(0, 100)}`);
			}
		}

		return {
			impactLevel: effectiveImpact,
			summary: summary || 'Completed code modifications.',
			details,
			relatedReeveDecisions,
			uncertainty,
			isArchitectureChange: effectiveImpact === ChangeImpactLevel.Architectural,
			beforeExplanation,
			afterExplanation,
		};
	}

	private createObservedAction(toolName: string, input: any): ObservedAction {
		const normName = String(toolName).toLowerCase();
		const cleanName = normName.replace(/^(copilot|vscode)_?/, '').replace(/[_-]/g, '');
		const actionId = `act_${++this.counter}_${Date.now()}`;

		// 1. File Reads and Searches (Internal / Informational)
		if (
			cleanName.includes('readfile') ||
			cleanName.includes('listdir') ||
			cleanName.includes('grepsearch') ||
			cleanName.includes('filesearch') ||
			cleanName.includes('semanticsearch') ||
			cleanName.includes('readprojectstructure') ||
			cleanName.includes('geterrors')
		) {
			return {
				id: actionId,
				category: ActionCategory.CodebaseSearch,
				toolName,
				targetResource: this.extractTargetResource(input),
				timestamp: Date.now(),
				impactLevel: ChangeImpactLevel.Trivial,
			};
		}

		// 2. File Creation
		if (cleanName.includes('createfile') || cleanName.includes('createnewworkspace')) {
			const filePath = input?.filePath || input?.targetFile || input?.path || 'new_file';
			const textContent = input?.content || input?.contents || input?.code || input?.text || '';
			const linesChanged = textContent ? textContent.split('\n').length : 1;
			const detectedRegex = this.detectComplexRegex(textContent);
			const hasComplexRegex = !!detectedRegex;
			const architecturalBoundary = this.detectArchitecturalBoundary(textContent);
			const structuralElements = this.extractStructuralElements(textContent);

			let impactLevel = ChangeImpactLevel.Normal;
			if (hasComplexRegex || !!architecturalBoundary || linesChanged > 10) {
				impactLevel = architecturalBoundary ? ChangeImpactLevel.Architectural : ChangeImpactLevel.Significant;
			}

			return {
				id: actionId,
				category: ActionCategory.FileCreate,
				toolName,
				targetResource: filePath,
				timestamp: Date.now(),
				impactLevel,
				linesChanged,
				hasComplexRegex,
				detectedRegex,
				architecturalBoundary,
				structuralElements,
				details: { filePath, linesChanged },
			};
		}

		// 3. File Edit / Replace / Patch
		if (
			cleanName.includes('insertedit') ||
			cleanName.includes('replacestring') ||
			cleanName.includes('applypatch') ||
			cleanName.includes('editnotebook') ||
			cleanName.includes('editfiles')
		) {
			const filePath = input?.filePath || input?.targetFile || input?.path || 'edited_file';
			const textContent =
				input?.replacementContent ||
				input?.patch ||
				input?.code ||
				input?.contents ||
				(Array.isArray(input?.edits) ? input.edits.map((e: any) => e.replacementContent || '').join('\n') : '');

			const linesChanged = textContent ? textContent.split('\n').length : 1;
			const detectedRegex = this.detectComplexRegex(textContent);
			const hasComplexRegex = !!detectedRegex;
			const architecturalBoundary = this.detectArchitecturalBoundary(textContent);
			const structuralElements = this.extractStructuralElements(textContent);

			let impactLevel = ChangeImpactLevel.Normal;
			if (linesChanged <= 4 && !hasComplexRegex && !architecturalBoundary) {
				impactLevel = ChangeImpactLevel.Trivial;
			} else if (linesChanged > 10 || hasComplexRegex || !!architecturalBoundary) {
				impactLevel = architecturalBoundary ? ChangeImpactLevel.Architectural : ChangeImpactLevel.Significant;
			}

			return {
				id: actionId,
				category: ActionCategory.FileEdit,
				toolName,
				targetResource: filePath,
				timestamp: Date.now(),
				impactLevel,
				linesChanged,
				hasComplexRegex,
				detectedRegex,
				architecturalBoundary,
				structuralElements,
				details: { filePath, linesChanged },
			};
		}

		// 4. Shell / Terminal Commands
		if (
			cleanName.includes('terminal') ||
			cleanName.includes('runtask') ||
			cleanName.includes('createandruntask')
		) {
			const command = input?.command || input?.cmd || input?.text || '';
			const isDestructive = DESTRUCTIVE_COMMAND_PATTERNS.some(p => p.test(command));
			const isNonObvious = NON_OBVIOUS_COMMAND_PATTERNS.some(p => p.test(command));

			// Check for deletion command specifically
			const isFileDeletion = /\b(rm\s|unlink\s|git\s+rm\b)/i.test(command);
			const category = isFileDeletion ? ActionCategory.FileDelete : ActionCategory.ShellCommand;

			let impactLevel = ChangeImpactLevel.Normal;
			if (isDestructive || isFileDeletion || isNonObvious) {
				impactLevel = ChangeImpactLevel.Significant;
			} else if (/^(git\s+status|pwd|ls|git\s+diff|echo\b)/i.test(command)) {
				impactLevel = ChangeImpactLevel.Trivial;
			}

			const targetResource = this.extractShellTarget(command);

			return {
				id: actionId,
				category,
				toolName,
				targetResource,
				timestamp: Date.now(),
				impactLevel,
				isDestructive,
				details: { command, isDestructive, isNonObvious, targetResource },
			};
		}

		// 5. Test executions
		if (normName.includes('test') || normName.includes('runtest')) {
			return {
				id: actionId,
				category: ActionCategory.TestRun,
				toolName,
				targetResource: input?.testName || input?.file || 'tests',
				timestamp: Date.now(),
				impactLevel: ChangeImpactLevel.Normal,
			};
		}

		// Fallback: Other/Internal
		return {
			id: actionId,
			category: ActionCategory.Other,
			toolName,
			timestamp: Date.now(),
			impactLevel: ChangeImpactLevel.Trivial,
		};
	}

	private extractTargetResource(input: any): string | undefined {
		if (!input) return undefined;
		if (typeof input === 'string') return input;
		return input.filePath || input.targetFile || input.path || input.command || input.query || undefined;
	}

	private detectComplexRegex(text: string): string | undefined {
		if (!text) return undefined;
		for (const pattern of COMPLEX_REGEX_PATTERNS) {
			const match = text.match(pattern);
			if (match) {
				return match[0].length > 40 ? match[0].slice(0, 37) + '...' : match[0];
			}
		}
		return undefined;
	}

	private detectArchitecturalBoundary(text: string): string | undefined {
		if (!text) return undefined;
		if (/\bclass\s+\w+Repository\b/i.test(text) || /\binterface\s+\w+Repository\b/i.test(text)) {
			return 'Repository';
		}
		if (/\bclass\s+\w+Middleware\b/i.test(text) || /\bfunction\s+\w+Middleware\b/i.test(text)) {
			return 'Middleware';
		}
		if (/\bclass\s+\w+Service\b/i.test(text) || /\binterface\s+\w+Service\b/i.test(text)) {
			return 'Service';
		}
		if (/\bclass\s+\w+Adapter\b/i.test(text) || /\binterface\s+\w+Adapter\b/i.test(text)) {
			return 'Adapter';
		}
		return undefined;
	}

	private extractShellTarget(command: string): string {
		const trimmed = command.trim();
		// Match rm [-flags] <target>
		const rmMatch = trimmed.match(/\b(?:rm|unlink|rimraf)\s+(?:-[a-zA-Z]+\s+)*([^\s;&|]+)/i);
		if (rmMatch && rmMatch[1]) {
			return rmMatch[1].replace(/['"]/g, '');
		}
		// Match git rm [-flags] <target>
		const gitRmMatch = trimmed.match(/\bgit\s+rm\s+(?:-[a-zA-Z]+\s+)*([^\s;&|]+)/i);
		if (gitRmMatch && gitRmMatch[1]) {
			return gitRmMatch[1].replace(/['"]/g, '');
		}
		// Match git reset / clean / checkout / restore
		if (/\bgit\s+(?:reset|clean|restore|checkout)\b/i.test(trimmed)) {
			const pathMatch = trimmed.match(/\bgit\s+(?:checkout|restore)\s+(?:--\s+)?([^\s;&|]+)/i);
			if (pathMatch && pathMatch[1] && !pathMatch[1].startsWith('-') && pathMatch[1] !== '.') {
				return pathMatch[1].replace(/['"]/g, '');
			}
			return 'repository working tree';
		}
		const tokens = trimmed.split(/\s+/).filter(t => !t.startsWith('-'));
		return tokens[1] || tokens[0] || 'target';
	}

	private extractStructuralElements(text: string): {
		interfaces: string[];
		classes: string[];
		functions: string[];
	} {
		const interfaces: string[] = [];
		const classes: string[] = [];
		const functions: string[] = [];
		if (!text) return { interfaces, classes, functions };

		const ifaceMatches = text.matchAll(/(?:export\s+)?interface\s+([A-Za-z0-9_]+)/g);
		for (const m of ifaceMatches) {
			interfaces.push(m[1]);
		}
		const classMatches = text.matchAll(/(?:export\s+)?class\s+([A-Za-z0-9_]+)/g);
		for (const m of classMatches) {
			classes.push(m[1]);
		}
		const fnMatches = text.matchAll(/(?:export\s+)?(?:async\s+)?function\s+([A-Za-z0-9_]+)/g);
		for (const m of fnMatches) {
			functions.push(m[1]);
		}
		return { interfaces, classes, functions };
	}

	private describeRegexPurpose(regexStr: string): string {
		if (/(?:https?|ftp):\/\/|\b(?:hostname|url|domain)\b/i.test(regexStr)) {
			return 'extract the hostname from the URL while ignoring the protocol and path';
		}
		if (/\b(?:email|mail)\b|@.*?\./i.test(regexStr)) {
			return 'validate email address formats';
		}
		if (/(?:lookahead|lookbehind|\(\?[=!<=])/i.test(regexStr) || /\/\(\?[=!<:][^/]+\//.test(regexStr)) {
			return 'extract structured tokens using lookaround assertions';
		}
		return 'match structured pattern tokens in the input';
	}
}
