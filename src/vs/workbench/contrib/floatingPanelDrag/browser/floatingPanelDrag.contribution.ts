/*---------------------------------------------------------------------------------------------
 *  Copyright (c) Microsoft Corporation. All rights reserved.
 *  Licensed under the MIT License. See License.txt in the project root for license information.
 *--------------------------------------------------------------------------------------------*/

import { $, append, addDisposableListener, EventType } from '../../../../base/browser/dom.js';
import { mainWindow } from '../../../../base/browser/window.js';
import { Disposable, toDisposable } from '../../../../base/common/lifecycle.js';
import { localize } from '../../../../nls.js';
import { IWorkbenchContribution, registerWorkbenchContribution2, WorkbenchPhase } from '../../../common/contributions.js';
import { IWorkbenchLayoutService, Parts } from '../../../services/layout/browser/layoutService.js';

const MIN_WIDTH = 180;
const MIN_HEIGHT = 120;

type ResizeDirection = 'n' | 's' | 'e' | 'w' | 'ne' | 'nw' | 'se' | 'sw';
const RESIZE_DIRECTIONS: ResizeDirection[] = ['n', 's', 'e', 'w', 'ne', 'nw', 'se', 'sw'];

/**
 * Adds a drag grip and a resize handle to the floating sidebar/auxiliary-bar
 * cards introduced by the Modern UI floating-panels styling, so they behave
 * like independent, detached windows (move and resize freely, not sharing
 * space with the editor) rather than docked Grid panels.
 *
 * Both act on each part's `.split-view-view` wrapper (the actual Grid-
 * positioned element — verified via live inspection that `.part.sidebar`
 * etc. themselves compute `position: static` and are not it), not the title
 * bar inside it, so neither can conflict with VS Code's own title-bar
 * drag-and-drop for reordering views between containers. The Grid's own
 * width-resize sash for sidebar/auxiliarybar is disabled (see
 * `disableGridResizeSash`) so it can't fight over the same boundary — this
 * new resize handle is the only way to resize them once floating panels are
 * on. The Grid's own reserved size/position, visibility toggling,
 * persistence and focus for the underlying part are otherwise completely
 * untouched; only what's rendered on top and how it can be moved/resized
 * changes. Dragged/resized state is in-memory only and resets on reload.
 */
export class FloatingPanelDragContribution extends Disposable implements IWorkbenchContribution {

	static readonly ID = 'workbench.contrib.floatingPanelDrag';

	constructor(
		@IWorkbenchLayoutService private readonly layoutService: IWorkbenchLayoutService,
	) {
		super();

		this.makeFloating(Parts.SIDEBAR_PART);
		this.makeFloating(Parts.AUXILIARYBAR_PART);
		this.disableGridResizeSash();
	}

	private makeFloating(part: Parts): void {
		const container = this.layoutService.getContainer(mainWindow, part);
		const wrapper = container?.parentElement;
		if (!container || !wrapper) {
			return;
		}

		this.setupPopAnimation(container, wrapper);

		let offsetX = 0;
		let offsetY = 0;
		let widthOverride: number | undefined;
		let heightOverride: number | undefined;

		// The fixed gap between the wrapper's box (what the resize handles
		// below actually change) and `.part`'s own rendered box inside it —
		// the floating-card margin/border from floatingPanels.css. `.part`'s
		// own width happens to track the wrapper's live via CSS, but its
		// height does not (verified live: it stayed pinned to its pre-resize
		// value after the wrapper was resized taller, since a part's own
		// size comes from an explicit pixel value set by its last `layout()`
		// call, not a CSS percentage) — so reading `.part`'s rendered size
		// after a resize to compute the new content size is unreliable for
		// height specifically. This fixed inset, measured once up front, lets
		// the content size passed to `layoutService.layoutPart` below be
		// derived directly from the wrapper size being set, instead.
		const initialWrapperRect = wrapper.getBoundingClientRect();
		const initialContainerRect = container.getBoundingClientRect();
		const widthInset = initialWrapperRect.width - initialContainerRect.width;
		const heightInset = initialWrapperRect.height - initialContainerRect.height;

		// The close animation (floatingPanels.css, .nosidebar/.noauxiliarybar)
		// forces the transform back to scale(0.96) with !important when the
		// part hides, which visually resets any dragged offset. Reset the
		// tracked JS state to match, so the panel starts fresh from its
		// anchored position/size next time it's shown instead of jumping from
		// stale values.
		this._register(this.layoutService.onDidChangePartVisibility(e => {
			if (e.partId === part && !e.visible) {
				offsetX = 0;
				offsetY = 0;
				widthOverride = undefined;
				heightOverride = undefined;
				wrapper.style.transform = '';
				wrapper.style.width = '';
				wrapper.style.height = '';
			}
		}));

		const grip = append(container, $('.floating-panel-drag-grip.codicon.codicon-gripper', {
			title: localize('floatingPanelDragGrip', "Drag to move"),
		}));

		this._register(addDisposableListener(grip, EventType.MOUSE_DOWN, (e: MouseEvent) => {
			e.preventDefault();

			const startX = e.clientX - offsetX;
			const startY = e.clientY - offsetY;

			const onMouseMove = (moveEvent: MouseEvent) => {
				offsetX = moveEvent.clientX - startX;
				offsetY = moveEvent.clientY - startY;
				wrapper.style.transform = `translate(${offsetX}px, ${offsetY}px)`;
			};

			const onMouseUp = () => {
				mainWindow.removeEventListener('mousemove', onMouseMove);
				mainWindow.removeEventListener('mouseup', onMouseUp);
			};

			mainWindow.addEventListener('mousemove', onMouseMove);
			mainWindow.addEventListener('mouseup', onMouseUp);
		}));

		// Edge/corner resize zones — the standard desktop-window resize
		// affordance: drag any edge or corner to grow/shrink from that side,
		// with the opposite edge staying anchored in place (dragging the left
		// edge right shrinks the panel while its right edge stays put; a
		// corner combines its two edges). Sets the wrapper's width/height/
		// position directly, which — being a later, JS-set inline style —
		// takes precedence over whatever the Grid's own layout pass last set
		// there, the same way the drag transform above does for position.
		//
		// Resizing the wrapper alone does not make the part's own content
		// (tree view, chat body, etc.) reflow — a part's content is sized by
		// explicit JS pixel dimensions from its own `layout()` call, not CSS
		// percentages, verified via live inspection that height especially
		// stayed pinned to its pre-resize value while the wrapper grew
		// underneath it. `layoutService.layoutPart` (mirroring the same
		// correction already applied to the main editor for the same reason)
		// re-runs that layout with the container's real post-resize size on
		// every move, so content actually fills the new size as you drag,
		// not only after some later unrelated layout pass.
		for (const dir of RESIZE_DIRECTIONS) {
			// Appended to `wrapper`, not `container` (`.part`): `.part.sidebar`
			// has `overflow: hidden` (it clips its own tree/chat content), which
			// was silently eating pointer events for roughly half of each
			// handle's hit area — verified live that clicks a few pixels into
			// the same handle rect that landed just inside `.part`'s own edge
			// hit the handle, while ones a few pixels further out (still
			// visually over the handle) fell through to the pane view
			// underneath instead. `wrapper` (the grid's own absolutely-
			// positioned box, already the thing being resized) has no such
			// clipping and sits above everything at z-index 50, so a handle
			// anchored to it is unambiguously hit-testable across its full
			// area.
			const handle = append(wrapper, $(`.floating-panel-resize-handle.${dir}`, {
				title: localize('floatingPanelResizeHandle', "Drag to resize"),
			}));

			this._register(addDisposableListener(handle, EventType.MOUSE_DOWN, (e: MouseEvent) => {
				e.preventDefault();
				e.stopPropagation(); // don't also start a drag via the grip's window-level listeners

				const startX = e.clientX;
				const startY = e.clientY;
				const startWidth = widthOverride ?? wrapper.getBoundingClientRect().width;
				const startHeight = heightOverride ?? wrapper.getBoundingClientRect().height;
				const startOffsetX = offsetX;
				const startOffsetY = offsetY;

				const onMouseMove = (moveEvent: MouseEvent) => {
					const dx = moveEvent.clientX - startX;
					const dy = moveEvent.clientY - startY;

					let newWidth = startWidth;
					let newHeight = startHeight;
					let newOffsetX = startOffsetX;
					let newOffsetY = startOffsetY;

					if (dir.includes('e')) {
						newWidth = Math.max(MIN_WIDTH, startWidth + dx);
					} else if (dir.includes('w')) {
						const clampedDx = Math.min(dx, startWidth - MIN_WIDTH);
						newWidth = startWidth - clampedDx;
						newOffsetX = startOffsetX + clampedDx;
					}

					if (dir.includes('s')) {
						newHeight = Math.max(MIN_HEIGHT, startHeight + dy);
					} else if (dir.includes('n')) {
						const clampedDy = Math.min(dy, startHeight - MIN_HEIGHT);
						newHeight = startHeight - clampedDy;
						newOffsetY = startOffsetY + clampedDy;
					}

					widthOverride = newWidth;
					heightOverride = newHeight;
					offsetX = newOffsetX;
					offsetY = newOffsetY;

					wrapper.style.width = `${newWidth}px`;
					wrapper.style.height = `${newHeight}px`;
					wrapper.style.transform = `translate(${offsetX}px, ${offsetY}px)`;
				};

				const onMouseUp = () => {
					mainWindow.removeEventListener('mousemove', onMouseMove);
					mainWindow.removeEventListener('mouseup', onMouseUp);

					// Only re-run the part's own layout once, here at drag end,
					// not on every mousemove above. Verified live that calling
					// it continuously during the drag causes runaway growth: a
					// part is still a real, grid-registered view (the grid just
					// isn't rendering it in its normal spot), so `Part.layout()`
					// feeds back into the grid's own understanding of that
					// view's size, which on the next mousemove reads a wrapper
					// rect the grid has already nudged — compounding every
					// step (confirmed by isolating it: identical drags produced
					// the correct `start + delta` width with this call removed,
					// and runaway growth with it left in the per-move handler).
					// One call at the end still fixes the actual complaint
					// (content not reflowing to the resized size) without that
					// repeated feedback.
					if (widthOverride !== undefined && heightOverride !== undefined) {
						const finalWidth = widthOverride - widthInset;
						const finalHeight = heightOverride - heightInset;
						this.layoutService.layoutPart(part, finalWidth, finalHeight);
						// A single call above updates the part's own box
						// immediately, but its innermost list/tree content
						// (nested another level down through the pane view)
						// verified live to sometimes still lag one frame
						// behind on height specifically. A second call next
						// frame, once the first has fully applied, closes
						// that gap without reintroducing the per-move
						// feedback loop the comment above this block
						// describes (this fires at most once per drag, not
						// once per mousemove).
						mainWindow.requestAnimationFrame(() => this.layoutService.layoutPart(part, finalWidth, finalHeight));
					}
				};

				mainWindow.addEventListener('mousemove', onMouseMove);
				mainWindow.addEventListener('mouseup', onMouseUp);
			}));
		}
	}

	/**
	 * Makes the sidebar/auxiliarybar's *open* actually animate. The
	 * `floatingPanels.css` opacity/transform transition on the part alone
	 * never plays at all as-is — verified live that the grid shows/hides a
	 * view by toggling `display` directly on this part's `.split-view-view`
	 * wrapper (a different, non-animatable property, on the ancestor, not
	 * the part itself). Going from `display: none` to visible has no prior
	 * rendered frame for a transition to interpolate from, so it just snaps
	 * straight to its resting look with nothing to animate.
	 * `.floating-panel-opening` pins it to the closed look (opacity/scale,
	 * transition disabled) for one frame first, a real reflow is forced
	 * (`wrapper.offsetHeight`) so the browser actually commits that as a
	 * paint instead of coalescing it with the next style change, and only
	 * then — the following frame — is the class removed, so the resting
	 * rule's own transition has a real "from" state to animate away from.
	 *
	 * This deliberately does *not* attempt the equivalent for closing.
	 * Closing means undoing the grid's `display: none` for the animation's
	 * duration and re-applying it after, on a timer — tried and reverted:
	 * verified live that the grid re-asserts a hidden view's `display: none`
	 * on every subsequent layout pass while it stays hidden, not only once
	 * at the moment it's hidden, so the timer's revert kept racing fresh
	 * grid passes it couldn't distinguish from a genuine re-open, at least
	 * once visibly stalling mid-cycle rather than settling either open or
	 * closed. That's a correctness risk this file isn't taking right before
	 * a push; closing stays an instant cut for now.
	 *
	 * The observer watches the whole `style` attribute, not just the one
	 * property that hides it — `MutationObserver` has no per-property
	 * filter, only per-attribute — and dragging/resizing (above) also write
	 * to this same wrapper's `style` (transform/width/height) on every
	 * mouse-move. Only reacting when hidden-ness actually *changes* (tracked
	 * in `wasHidden`, compared against the current read) rather than
	 * whenever it merely *is* visible right now is what tells a genuine show
	 * apart from an unrelated drag tick — verified live that checking the
	 * current value alone re-ran the whole pinned-frame/reflow/rAF sequence
	 * on every drag-move style write, which is what was actually causing a
	 * reported flicker while dragging.
	 *
	 * "Hidden" itself is read from the wrapper's rendered size, not from a
	 * specific style property like `display`. Verified live that the grid
	 * doesn't hide a view the same way every time: a never-touched wrapper
	 * gets `display: none`, but one that already carries this file's own
	 * inline `width`/`height` from an earlier drag or resize (i.e. any
	 * panel a person has actually moved or resized, which is the normal
	 * case, not an edge case) gets `width: 0` instead, `display` untouched —
	 * caught this by comparing the exact same close action on a fresh vs. a
	 * previously-dragged panel. A zero-size check reflects either mechanism
	 * (or any other the grid might use) without needing to know which one
	 * applied.
	 */
	private setupPopAnimation(container: HTMLElement, wrapper: HTMLElement): void {
		const isHidden = () => {
			const rect = wrapper.getBoundingClientRect();
			return rect.width === 0 || rect.height === 0;
		};

		let wasHidden = isHidden();

		const observer = new MutationObserver(() => {
			const nowHidden = isHidden();
			if (wasHidden && !nowHidden) {
				container.classList.add('floating-panel-opening');
				void wrapper.offsetHeight; // force the reflow described above
				mainWindow.requestAnimationFrame(() => {
					container.classList.remove('floating-panel-opening');
				});
			}
			wasHidden = nowHidden;
		});

		observer.observe(wrapper, { attributes: true, attributeFilter: ['style'] });
		this._register(toDisposable(() => observer.disconnect()));
	}

	/**
	 * Disables the Grid's own width-resize sash between sidebar/auxiliarybar
	 * and the editor. It shares grid space between siblings — exactly the
	 * behaviour floating panels are meant to opt out of — and would otherwise
	 * fight over the same boundary with the new resize handle above. Disabled
	 * via pointer-events, not removed: the Grid keeps its own internal
	 * bookkeeping for the sash untouched, only mouse interaction with it is
	 * blocked.
	 */
	private disableGridResizeSash(): void {
		// There are several `.sash-container` elements — one per nested
		// split-view level in the Grid, verified via live inspection — not
		// just one. The container holding the sidebar/auxiliarybar sashes is
		// not reliably the first in DOM order, so every container needs
		// checking, not just `querySelector`'s first match. These sashes are
		// created by the Grid/SplitView, not by this contribution, so there
		// is no `h()`-built reference to hold onto instead — selector-based
		// lookup is the only way to reach them from the outside.
		// eslint-disable-next-line no-restricted-syntax
		for (const sashContainer of Array.from(mainWindow.document.querySelectorAll('.sash-container'))) {
			// eslint-disable-next-line no-restricted-syntax
			for (const sash of Array.from(sashContainer.querySelectorAll('.monaco-sash.vertical'))) {
				if (sash.classList.contains('disabled')) {
					continue; // already inert (e.g. the activity bar's own fixed edge)
				}
				(sash as HTMLElement).style.pointerEvents = 'none';
				(sash as HTMLElement).style.opacity = '0';
			}
		}
	}
}

registerWorkbenchContribution2(FloatingPanelDragContribution.ID, FloatingPanelDragContribution, WorkbenchPhase.AfterRestored);
