/*---------------------------------------------------------------------------------------------
 *  Copyright (c) Microsoft Corporation. All rights reserved.
 *  Licensed under the MIT License. See License.txt in the project root for license information.
 *--------------------------------------------------------------------------------------------*/

import { $, append, addDisposableListener, EventType, clearNode } from '../../../../base/browser/dom.js';
import { mainWindow } from '../../../../base/browser/window.js';
import { autorun } from '../../../../base/common/observable.js';
import { Disposable } from '../../../../base/common/lifecycle.js';
import { localize } from '../../../../nls.js';
import { IWorkbenchContribution, registerWorkbenchContribution2, WorkbenchPhase } from '../../../common/contributions.js';
import { INotificationService } from '../../../../platform/notification/common/notification.js';
import { ITimelineCommit, IVersionTimelineService } from './versionTimelineService.js';

const MIN_WIDTH = 420;
const MIN_HEIGHT = 220;

type ResizeDirection = 'n' | 's' | 'e' | 'w' | 'ne' | 'nw' | 'se' | 'sw';
const RESIZE_DIRECTIONS: ResizeDirection[] = ['n', 's', 'e', 'w', 'ne', 'nw', 'se', 'sw'];

function formatTimestamp(timestamp: number): string {
	return new Date(timestamp).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' });
}

/**
 * Floating glass window for the version Timeline feature — a horizontal row
 * of past commits the user can pan through left/right (no vertical or zoom
 * gestures, unlike the Architecture Diagram window, since a single
 * horizontal axis of time is all this view represents) and click into for a
 * plain-language explanation or to check that version out directly.
 */
class VersionTimelineContribution extends Disposable implements IWorkbenchContribution {

	static readonly ID = 'workbench.contrib.versionTimeline';

	private window: HTMLElement | undefined;
	private body: HTMLElement | undefined;
	private refreshButton: HTMLElement | undefined;
	private popup: HTMLElement | undefined;

	constructor(
		@IVersionTimelineService private readonly timelineService: IVersionTimelineService,
		@INotificationService private readonly notificationService: INotificationService,
	) {
		super();

		this._register(this.timelineService.onDidRequestOpen(() => this.show()));
		this._register(autorun(reader => {
			const state = this.timelineService.state.read(reader);
			this.renderState(state);
		}));
	}

	private ensureWindow(): { window: HTMLElement; body: HTMLElement } {
		if (this.window && this.body) {
			return { window: this.window, body: this.body };
		}

		// The workbench root is created by the workbench shell, not by this
		// contribution, so there is no `h()`-built reference to hold onto
		// instead — selector-based lookup is the only way to reach it.
		// eslint-disable-next-line no-restricted-syntax
		const container = mainWindow.document.querySelector<HTMLElement>('.monaco-workbench');
		if (!container) {
			throw new Error('Workbench container not found');
		}

		const win = append(container, $('.version-timeline-window'));
		win.style.width = '820px';
		win.style.height = '300px';
		win.style.left = `${Math.max(80, (mainWindow.innerWidth - 820) / 2)}px`;
		win.style.top = `${Math.max(60, mainWindow.innerHeight - 420)}px`;

		const header = append(win, $('.version-timeline-header'));
		append(header, $('.floating-panel-drag-grip.codicon.codicon-gripper', { title: localize('versionTimelineDrag', "Drag to move") }));
		append(header, $('.version-timeline-title', undefined, localize('versionTimelineTitle', "Timeline")));

		const refresh = append(header, $('button.version-timeline-action.codicon.codicon-refresh', { title: localize('versionTimelineRefresh', "Refresh") }));
		this._register(addDisposableListener(refresh, EventType.CLICK, () => this.timelineService.refresh()));
		this.refreshButton = refresh;

		const close = append(header, $('button.version-timeline-action.codicon.codicon-close', { title: localize('versionTimelineClose', "Close") }));
		this._register(addDisposableListener(close, EventType.CLICK, () => this.hide()));

		const body = append(win, $('.version-timeline-body'));

		this.setupDrag(win, header);
		this.setupResize(win);

		this.window = win;
		this.body = body;
		return { window: win, body };
	}

	private show(): void {
		const { window: win } = this.ensureWindow();
		if (win.classList.contains('visible')) {
			return;
		}
		win.style.display = 'flex';
		win.classList.add('opening');
		void win.offsetHeight; // force a reflow so the "opening" (pinned, transition-disabled) look actually paints before it's removed
		mainWindow.requestAnimationFrame(() => {
			win.classList.remove('opening');
			win.classList.add('visible');
		});
	}

	private hide(): void {
		const win = this.window;
		if (!win) {
			return;
		}
		this.closePopup();
		win.classList.remove('visible');
		const onTransitionEnd = () => {
			win.style.display = 'none';
		};
		win.addEventListener('transitionend', onTransitionEnd, { once: true });
	}

	private renderState(state: ReturnType<IVersionTimelineService['state']['get']>): void {
		if (this.refreshButton) {
			this.refreshButton.classList.toggle('spinning', state.status === 'loading');
		}

		if (!this.window) {
			return;
		}
		const { body } = this.ensureWindow();
		this.closePopup();

		if (state.status === 'loading' && !state.commits) {
			clearNode(body);
			append(body, $('.version-timeline-status', undefined, localize('versionTimelineLoading', "Loading commit history…")));
			return;
		}

		if (state.status === 'error') {
			clearNode(body);
			append(body, $('.version-timeline-status.error', undefined, state.errorMessage ?? localize('versionTimelineError', "Something went wrong.")));
			return;
		}

		if (state.status === 'ready' && state.commits) {
			this.renderTrack(body, state.commits);
		}
	}

	private renderTrack(body: HTMLElement, commits: readonly ITimelineCommit[]): void {
		clearNode(body);

		const track = append(body, $('.version-timeline-track'));

		const nodes = append(track, $('.version-timeline-nodes'));
		// The connecting line has to live inside `.nodes`, not `.track`: as
		// an absolutely-positioned child it's sized against its containing
		// block's own layout width, and `.track` is the fixed-width
		// scroll *viewport* — sizing the line against it left it stopping at
		// the visible edge instead of spanning the full scrollable content
		// width that `.nodes` (width: max-content) actually has.
		append(nodes, $('.version-timeline-line'));
		for (const commit of commits) {
			const node = append(nodes, $('.version-timeline-node'));
			append(node, $('.version-timeline-node-time', undefined, formatTimestamp(commit.timestamp)));
			const box = append(node, $('.version-timeline-node-box', { title: commit.message }, commit.title || commit.shortId));

			this._register(addDisposableListener(box, EventType.CLICK, () => this.openPopup(box, commit)));
		}

		this.setupTrackPanning(body, track);

		// Land on the most recent commit (the right edge) rather than the
		// oldest, since "where am I now" matters more on first open than
		// scrolling through the whole history immediately.
		track.scrollLeft = track.scrollWidth;
	}

	private setupTrackPanning(body: HTMLElement, track: HTMLElement): void {
		this._register(addDisposableListener(track, EventType.WHEEL, (e: WheelEvent) => {
			if (track.scrollWidth <= track.clientWidth) {
				return;
			}
			e.preventDefault();
			track.scrollLeft += Math.abs(e.deltaX) > Math.abs(e.deltaY) ? e.deltaX : e.deltaY;
		}, { passive: false }));

		this._register(addDisposableListener(track, EventType.POINTER_DOWN, (e: PointerEvent) => {
			if (e.button !== 0) {
				return;
			}
			// A commit box's own click handler doesn't stop propagation (a
			// plain click has no meaningful drag distance to distinguish),
			// but a real drag gesture is only ever started from the empty
			// track background in practice — panning while grabbing a box is
			// not a gesture this timeline needs to support.
			if ((e.target as HTMLElement).closest('.version-timeline-node-box')) {
				return;
			}
			track.setPointerCapture(e.pointerId);
			track.classList.add('panning');
			const startClientX = e.clientX;
			const startScrollLeft = track.scrollLeft;

			let cleaned = false;
			const cleanup = () => {
				if (cleaned) {
					return;
				}
				cleaned = true;
				track.classList.remove('panning');
				track.removeEventListener('pointermove', onMove);
				track.removeEventListener('pointerup', onUp);
				track.removeEventListener('pointercancel', onUp);
				track.removeEventListener('lostpointercapture', cleanup);
			};
			const onMove = (moveEvent: PointerEvent) => {
				track.scrollLeft = startScrollLeft - (moveEvent.clientX - startClientX);
			};
			const onUp = (upEvent: PointerEvent) => {
				if (track.hasPointerCapture(upEvent.pointerId)) {
					track.releasePointerCapture(upEvent.pointerId);
				}
				cleanup();
			};
			track.addEventListener('pointermove', onMove);
			track.addEventListener('pointerup', onUp);
			track.addEventListener('pointercancel', onUp);
			track.addEventListener('lostpointercapture', cleanup);
		}));
	}

	private closePopup(): void {
		this.popup?.remove();
		this.popup = undefined;
	}

	private openPopup(box: HTMLElement, commit: ITimelineCommit): void {
		const win = this.window;
		if (!win) {
			return;
		}
		if (this.popup?.dataset.commitId === commit.id) {
			this.closePopup();
			return;
		}
		this.closePopup();

		const popup = append(win, $('.version-timeline-popup'));
		popup.dataset.commitId = commit.id;
		this.popup = popup;

		const header = append(popup, $('.version-timeline-popup-header'));
		append(header, $('span', undefined, `${commit.shortId} — ${commit.title}`));
		const closeButton = append(header, $('button.version-timeline-popup-close.codicon.codicon-close', { title: localize('versionTimelinePopupClose', "Close") }));
		this._register(addDisposableListener(closeButton, EventType.CLICK, () => this.closePopup()));

		const actions = append(popup, $('.version-timeline-popup-actions'));
		const infoButton = append(actions, $('button.version-timeline-popup-button', undefined, localize('versionTimelineInfo', "Info")));
		const teleportButton = append(actions, $('button.version-timeline-popup-button', undefined, localize('versionTimelineTeleport', "Teleport")));

		const resultBody = append(popup, $('.version-timeline-popup-result'));

		this._register(addDisposableListener(infoButton, EventType.CLICK, async () => {
			clearNode(resultBody);
			append(resultBody, $('span', undefined, localize('versionTimelineExplaining', "Reading the commit…")));
			try {
				const explanation = await this.timelineService.explainCommit(commit.id);
				clearNode(resultBody);
				append(resultBody, $('span', undefined, explanation));
			} catch (e) {
				clearNode(resultBody);
				append(resultBody, $('span.version-timeline-popup-error', undefined, e instanceof Error ? e.message : String(e)));
			}
		}));

		this._register(addDisposableListener(teleportButton, EventType.CLICK, async () => {
			clearNode(resultBody);
			append(resultBody, $('span', undefined, localize('versionTimelineTeleporting', "Checking out {0}…", commit.shortId)));
			try {
				await this.timelineService.teleportToCommit(commit.id);
				clearNode(resultBody);
				append(resultBody, $('span', undefined, localize('versionTimelineTeleported', "Checked out {0} (detached HEAD). Create a branch from here if you want to keep working on it.", commit.shortId)));
				this.notificationService.info(localize('versionTimelineTeleportedNotification', "Checked out commit {0}.", commit.shortId));
			} catch (e) {
				clearNode(resultBody);
				append(resultBody, $('span.version-timeline-popup-error', undefined, e instanceof Error ? e.message : String(e)));
			}
		}));

		// Position above or below the clicked box depending on available
		// room, centered on it and clamped to stay inside the window.
		const winRect = win.getBoundingClientRect();
		const boxRect = box.getBoundingClientRect();
		void popup.offsetHeight;
		const popupWidth = popup.offsetWidth || 260;
		const popupHeight = popup.offsetHeight || 120;

		const spaceAbove = boxRect.top - winRect.top;
		const openAbove = spaceAbove > popupHeight + 16;

		let left = boxRect.left - winRect.left + boxRect.width / 2 - popupWidth / 2;
		left = Math.max(8, Math.min(left, winRect.width - popupWidth - 8));
		const top = openAbove
			? spaceAbove - popupHeight - 12
			: (boxRect.bottom - winRect.top) + 12;

		popup.style.left = `${left}px`;
		popup.style.top = `${Math.max(8, top)}px`;
	}

	private setupDrag(win: HTMLElement, grip: HTMLElement): void {
		this._register(addDisposableListener(grip, EventType.MOUSE_DOWN, (e: MouseEvent) => {
			e.preventDefault();
			const rect = win.getBoundingClientRect();
			const startX = e.clientX - rect.left;
			const startY = e.clientY - rect.top;

			const onMouseMove = (moveEvent: MouseEvent) => {
				win.style.left = `${moveEvent.clientX - startX}px`;
				win.style.top = `${moveEvent.clientY - startY}px`;
			};
			const onMouseUp = () => {
				mainWindow.removeEventListener('mousemove', onMouseMove);
				mainWindow.removeEventListener('mouseup', onMouseUp);
			};
			mainWindow.addEventListener('mousemove', onMouseMove);
			mainWindow.addEventListener('mouseup', onMouseUp);
		}));
	}

	private setupResize(win: HTMLElement): void {
		for (const dir of RESIZE_DIRECTIONS) {
			const handle = append(win, $(`.floating-panel-resize-handle.${dir}`, { title: localize('versionTimelineResize', "Drag to resize") }));

			this._register(addDisposableListener(handle, EventType.MOUSE_DOWN, (e: MouseEvent) => {
				e.preventDefault();
				e.stopPropagation();

				const rect = win.getBoundingClientRect();
				const startX = e.clientX;
				const startY = e.clientY;
				const startLeft = rect.left;
				const startTop = rect.top;
				const startWidth = rect.width;
				const startHeight = rect.height;

				const onMouseMove = (moveEvent: MouseEvent) => {
					const dx = moveEvent.clientX - startX;
					const dy = moveEvent.clientY - startY;

					let newLeft = startLeft;
					let newTop = startTop;
					let newWidth = startWidth;
					let newHeight = startHeight;

					if (dir.includes('e')) {
						newWidth = Math.max(MIN_WIDTH, startWidth + dx);
					} else if (dir.includes('w')) {
						const clampedDx = Math.min(dx, startWidth - MIN_WIDTH);
						newWidth = startWidth - clampedDx;
						newLeft = startLeft + clampedDx;
					}

					if (dir.includes('s')) {
						newHeight = Math.max(MIN_HEIGHT, startHeight + dy);
					} else if (dir.includes('n')) {
						const clampedDy = Math.min(dy, startHeight - MIN_HEIGHT);
						newHeight = startHeight - clampedDy;
						newTop = startTop + clampedDy;
					}

					win.style.left = `${newLeft}px`;
					win.style.top = `${newTop}px`;
					win.style.width = `${newWidth}px`;
					win.style.height = `${newHeight}px`;
				};
				const onMouseUp = () => {
					mainWindow.removeEventListener('mousemove', onMouseMove);
					mainWindow.removeEventListener('mouseup', onMouseUp);
				};
				mainWindow.addEventListener('mousemove', onMouseMove);
				mainWindow.addEventListener('mouseup', onMouseUp);
			}));
		}
	}
}

registerWorkbenchContribution2(VersionTimelineContribution.ID, VersionTimelineContribution, WorkbenchPhase.AfterRestored);
