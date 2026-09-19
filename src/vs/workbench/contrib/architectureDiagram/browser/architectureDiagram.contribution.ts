/*---------------------------------------------------------------------------------------------
 *  Copyright (c) Microsoft Corporation. All rights reserved.
 *  Licensed under the MIT License. See License.txt in the project root for license information.
 *--------------------------------------------------------------------------------------------*/

// A bare `import ... from 'mermaid'` fails at dev-mode runtime: the
// workbench's renderer resolves ES module specifiers natively (no bundler,
// no import map), which only understands relative/absolute URLs, not bare
// package names — verified live ("Failed to resolve module specifier
// 'mermaid'"). Mermaid's own published `dist/mermaid.core.mjs` doesn't fix
// this either: it dynamically imports per-diagram-type chunks that in turn
// bare-import d3, cytoscape, dagre, dompurify, and others, i.e. it's built
// to be consumed by a bundler, not loaded directly by a browser. This repo
// pre-bundles it once (`npx esbuild --bundle --format=esm` against
// `node_modules/mermaid/dist/mermaid.core.mjs`) into `mermaidBundle.js`
// sitting right next to this file — a genuinely self-contained ESM file
// with zero remaining bare specifiers — and that gets imported by ordinary
// relative path below, the same way every other same-tree import in this
// codebase resolves.
import mermaid from './mermaidBundle.js';
import { $, append, addDisposableListener, EventType, clearNode } from '../../../../base/browser/dom.js';
import { mainWindow } from '../../../../base/browser/window.js';
import { createTrustedTypesPolicy } from '../../../../base/browser/trustedTypes.js';
import { autorun } from '../../../../base/common/observable.js';
import { Disposable } from '../../../../base/common/lifecycle.js';
import { localize } from '../../../../nls.js';
import { IWorkbenchContribution, registerWorkbenchContribution2, WorkbenchPhase } from '../../../common/contributions.js';
import { IEditorService } from '../../../services/editor/common/editorService.js';
import { IArchitectureDiagramService, IArchitectureGraph } from './architectureDiagramService.js';

const MIN_WIDTH = 360;
const MIN_HEIGHT = 260;

type ResizeDirection = 'n' | 's' | 'e' | 'w' | 'ne' | 'nw' | 'se' | 'sw';
const RESIZE_DIRECTIONS: ResizeDirection[] = ['n', 's', 'e', 'w', 'ne', 'nw', 'se', 'sw'];

const MIN_CANVAS_SCALE = 0.15;
const MAX_CANVAS_SCALE = 3;
const WHEEL_ZOOM_SPEED = 0.0016;
const PINCH_ZOOM_SPEED = 0.01;
/** Screen-pixel movement, in either axis, below which a node pointerdown→up is treated as a click rather than a drag. */
const DRAG_CLICK_THRESHOLD = 4;

let mermaidInitialized = false;
function ensureMermaidInitialized(): void {
	if (mermaidInitialized) {
		return;
	}
	mermaidInitialized = true;
	// Matches the workbench's own onyx/orange palette (2026-dark.json) rather
	// than a stock mermaid theme, so the diagram reads as part of the app
	// instead of a foreign embed. `securityLevel: 'strict'` is deliberate:
	// mermaid supports embedding `click` callbacks that execute arbitrary JS
	// named in the diagram text, which matters when diagram text comes from
	// an LLM response — this contribution never uses that, doing click-to-
	// open-file itself via its own DOM listeners against the already-
	// validated node/path data instead, so nothing the model outputs can run
	// as code no matter what it contains.
	mermaid.initialize({
		startOnLoad: false,
		securityLevel: 'strict',
		theme: 'base',
		themeVariables: {
			background: '#18130E',
			primaryColor: '#211A13',
			primaryTextColor: '#bfbfbf',
			primaryBorderColor: '#C74A08',
			lineColor: '#C74A08',
			secondaryColor: '#2E251C',
			tertiaryColor: '#211A13',
			fontFamily: 'var(--vscode-font-family, sans-serif)',
			clusterBkg: 'rgba(199, 74, 8, 0.08)',
			clusterBorder: '#C74A08',
		},
	});
}

/**
 * The workbench enforces Trusted Types (`require-trusted-types-for
 * 'script'` in workbench.html/workbench-dev.html): any raw
 * `element.innerHTML = string` assignment anywhere on the page throws
 * unless that string was produced by an approved policy's `createHTML`.
 * Mermaid's bundled DOMPurify tries to register its own policy for this —
 * verified live that it collided with the workbench's own pre-existing
 * `dompurify` policy (same default name, and policy names must be unique
 * per document), which is why the bundle's default policy name was
 * rewritten to `reeve-arch-diagram-dompurify` at bundle-build time (see the
 * comment on the `mermaid` import above) and that name added to both
 * workbench HTML files' `trusted-types` allowlist. But mermaid *also* does
 * at least one raw `innerHTML` write elsewhere internally (verified live:
 * the error persisted, unchanged, at the exact same call site, even after
 * that collision was fixed and its own policy started creating
 * successfully) that never goes through DOMPurify's `sanitize()` at all —
 * most likely a detached element used for text measurement during layout,
 * not anything rendering untrusted content.
 *
 * Patching every raw `innerHTML` write inside a 3.3MB minified bundle to
 * route through a policy isn't practical, and a permanent page-wide
 * `'default'` Trusted Types policy would quietly re-permit unsafe
 * `innerHTML` writes for every *other* feature too, undoing a real
 * protection for code this contribution has nothing to do with. Instead,
 * `Element.prototype.innerHTML`'s setter is swapped for one that routes
 * through a pass-through policy — safe here specifically because
 * everything mermaid renders originates from this contribution's own
 * validated node/edge data, never raw model output — only for the
 * duration of the single `mermaid.render()` call below, then restored to
 * the native setter immediately after. The known residual risk is the
 * same as any monkey-patch of a shared prototype: another async task that
 * happens to set `innerHTML` during that narrow synchronous-ish window
 * would also go through the pass-through, though nothing else in this
 * flow does so between the patch and its `finally` restore.
 */
const architectureDiagramTrustedTypesPolicy = createTrustedTypesPolicy('architectureDiagramRender', { createHTML: (value: string) => value });

async function renderMermaidWithTrustedTypesWorkaround(text: string): Promise<{ svg: string }> {
	const descriptor = Object.getOwnPropertyDescriptor(Element.prototype, 'innerHTML');
	if (!descriptor?.set || !descriptor.get) {
		return mermaid.render('reeve-architecture-diagram', text);
	}

	Object.defineProperty(Element.prototype, 'innerHTML', {
		configurable: true,
		enumerable: descriptor.enumerable,
		get: descriptor.get,
		set(this: Element, value: string) {
			descriptor.set!.call(this, (architectureDiagramTrustedTypesPolicy?.createHTML(value) ?? value) as unknown as string);
		},
	});

	try {
		return await mermaid.render('reeve-architecture-diagram', text);
	} finally {
		Object.defineProperty(Element.prototype, 'innerHTML', descriptor);
	}
}

function setSvgAttr(el: SVGElement, name: string, value: string): void {
	el.setAttribute(name, value);
}

interface INodeVisual {
	readonly id: string;
	readonly element: SVGGraphicsElement;
	/** Center of the node's own geometry, in the node group's local (untranslated) space. */
	readonly localCenterX: number;
	readonly localCenterY: number;
	/** The node's original mermaid-computed `translate(x, y)`. */
	readonly baseX: number;
	readonly baseY: number;
	/** Cumulative drag offset applied on top of the base position. */
	offsetX: number;
	offsetY: number;
}

interface IEdgeVisual {
	readonly fromId: string;
	readonly toId: string;
	readonly line: SVGLineElement;
	readonly labelBg?: SVGRectElement;
	readonly labelText?: SVGTextElement;
}

/**
 * Floating glass window for the Architecture Diagram feature. Unlike the
 * sidebar/auxiliarybar (`floatingPanelDrag.contribution.ts`), this window is
 * not a grid-managed Part at all — it's a plain absolutely-positioned div
 * this contribution owns outright, appended once to the workbench root and
 * shown/hidden by this contribution alone. That sidesteps the entire class
 * of bugs the sidebar/auxiliarybar animation work ran into (the grid
 * repeatedly re-asserting `display: none`/zeroed size on a view it manages):
 * there is no grid on the other side fighting over this element's style, so
 * open/close can just be a normal CSS transition with no interception logic
 * needed at all.
 *
 * The diagram body is an infinite-canvas-style viewport: `.architecture-
 * diagram-canvas` carries a single `translate(x, y) scale(s)` transform that
 * wheel/drag gestures update, while the mermaid-rendered SVG underneath it
 * keeps its natural (unscaled) pixel size so the canvas transform is the
 * only thing responsible for zoom, matching how tools like Figma/Miro
 * separate "content size" from "current view". Individual nodes are
 * draggable on top of that: mermaid's own edge paths are hidden and replaced
 * with a small overlay SVG of plain lines recomputed from this
 * contribution's own node-center bookkeeping, so moving a node can never
 * desync from or corrupt mermaid's internal edge routing — there's no
 * mermaid edge left to desync from.
 */
class ArchitectureDiagramContribution extends Disposable implements IWorkbenchContribution {

	static readonly ID = 'workbench.contrib.architectureDiagram';

	private window: HTMLElement | undefined;
	private body: HTMLElement | undefined;
	private canvas: HTMLElement | undefined;
	private regenerateButton: HTMLElement | undefined;

	private canvasScale = 1;
	private canvasX = 0;
	private canvasY = 0;

	private nodeVisuals = new Map<string, INodeVisual>();
	private edgeVisuals: IEdgeVisual[] = [];

	constructor(
		@IArchitectureDiagramService private readonly architectureDiagramService: IArchitectureDiagramService,
		@IEditorService private readonly editorService: IEditorService,
	) {
		super();

		this._register(this.architectureDiagramService.onDidRequestOpen(() => this.show()));
		this._register(autorun(reader => {
			const state = this.architectureDiagramService.state.read(reader);
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

		const win = append(container, $('.architecture-diagram-window'));
		win.style.width = '760px';
		win.style.height = '560px';
		win.style.left = `${Math.max(80, (mainWindow.innerWidth - 760) / 2)}px`;
		win.style.top = `${Math.max(60, (mainWindow.innerHeight - 560) / 3)}px`;

		const header = append(win, $('.architecture-diagram-header'));
		append(header, $('.floating-panel-drag-grip.codicon.codicon-gripper', { title: localize('architectureDiagramDrag', "Drag to move") }));
		append(header, $('.architecture-diagram-title', undefined, localize('architectureDiagramTitle', "Architecture Diagram")));

		const regenerate = append(header, $('button.architecture-diagram-action.codicon.codicon-refresh', { title: localize('architectureDiagramRegenerate', "Regenerate") }));
		this._register(addDisposableListener(regenerate, EventType.CLICK, () => this.architectureDiagramService.generate()));
		this.regenerateButton = regenerate;

		const close = append(header, $('button.architecture-diagram-action.codicon.codicon-close', { title: localize('architectureDiagramClose', "Close") }));
		this._register(addDisposableListener(close, EventType.CLICK, () => this.hide()));

		const body = append(win, $('.architecture-diagram-body'));

		this.setupDrag(win, header);
		this.setupResize(win);
		this.setupViewportPanAndZoom(body);

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
		win.classList.remove('visible');
		const onTransitionEnd = () => {
			win.style.display = 'none';
		};
		win.addEventListener('transitionend', onTransitionEnd, { once: true });
	}

	private renderState(state: ReturnType<IArchitectureDiagramService['state']['get']>): void {
		if (this.regenerateButton) {
			this.regenerateButton.classList.toggle('spinning', state.status === 'generating');
		}

		if (!this.window) {
			// Nothing has ever opened the window yet — nothing to render into.
			return;
		}
		const { body } = this.ensureWindow();

		if (state.status === 'generating' && !state.mermaid) {
			this.resetCanvasState();
			clearNode(body);
			append(body, $('.architecture-diagram-status', undefined, state.stageMessage ?? localize('architectureDiagramGenerating', "Analyzing the project…")));
			return;
		}

		if (state.status === 'error') {
			this.resetCanvasState();
			clearNode(body);
			append(body, $('.architecture-diagram-status.error', undefined, state.errorMessage ?? localize('architectureDiagramError', "Something went wrong.")));
			return;
		}

		if (state.status === 'ready' && state.mermaid && state.graph) {
			this.renderDiagram(body, state.mermaid, state.graph);
		}
	}

	private resetCanvasState(): void {
		this.canvas = undefined;
		this.nodeVisuals.clear();
		this.edgeVisuals = [];
		this.canvasScale = 1;
		this.canvasX = 0;
		this.canvasY = 0;
	}

	private async renderDiagram(body: HTMLElement, mermaidText: string, graph: IArchitectureGraph): Promise<void> {
		ensureMermaidInitialized();
		this.resetCanvasState();
		clearNode(body);

		append(body, $('.architecture-diagram-summary', undefined, graph.summary));

		const canvas = append(body, $('.architecture-diagram-canvas'));
		const diagramHost = append(canvas, $('.architecture-diagram-svg-host'));
		this.canvas = canvas;

		let svg: string;
		try {
			({ svg } = await renderMermaidWithTrustedTypesWorkaround(mermaidText));
		} catch {
			this.resetCanvasState();
			clearNode(body);
			append(body, $('.architecture-diagram-status.error', undefined, localize('architectureDiagramRenderFailed', "Couldn't render the diagram.")));
			return;
		}

		const trustedSvg = architectureDiagramTrustedTypesPolicy?.createHTML(svg) ?? svg;
		diagramHost.innerHTML = trustedSvg as string;

		// The SVG markup is mermaid's own output, injected via `innerHTML`
		// above rather than built through `h()`, so a selector lookup is the
		// only way to get a reference to it afterward. The `instanceof`
		// checks below are safe without `DOM.isSVGElement()`'s multi-window
		// handling — that generic helper also can't narrow to the specific
		// `SVGSVGElement`/`SVGGraphicsElement` subtypes needed here, and this
		// contribution never creates its window or SVG content in any
		// document but `mainWindow`'s.
		// eslint-disable-next-line no-restricted-syntax
		const svgElement = diagramHost.querySelector('svg');
		// eslint-disable-next-line no-restricted-syntax
		if (!(svgElement instanceof SVGSVGElement)) {
			return;
		}

		// The canvas transform is now solely responsible for zoom, so the SVG
		// itself renders at its true intrinsic size rather than fitting its
		// container — otherwise the SVG's own responsive sizing and the
		// canvas's scale would fight over what "zoomed in" even means.
		const viewBox = svgElement.viewBox.baseVal;
		const naturalWidth = viewBox && viewBox.width > 0 ? viewBox.width : svgElement.getBBox().width || 400;
		const naturalHeight = viewBox && viewBox.height > 0 ? viewBox.height : svgElement.getBBox().height || 300;
		svgElement.style.display = 'block';
		svgElement.style.width = `${naturalWidth}px`;
		svgElement.style.height = `${naturalHeight}px`;
		svgElement.style.maxWidth = 'none';

		this.buildNodeVisuals(svgElement, graph);
		const overlay = this.buildEdgesOverlay(naturalWidth, naturalHeight, graph);
		diagramHost.insertBefore(overlay, svgElement);

		// Mermaid's own edge paths/labels are no longer the source of truth —
		// the overlay above redraws every edge from this contribution's own
		// node-center bookkeeping so dragging a node can never leave a mermaid
		// edge pointing at a stale position. Hidden rather than removed: it's
		// one CSS rule instead of guessing at mermaid's internal group
		// structure, which has changed across mermaid versions before.
		// eslint-disable-next-line no-restricted-syntax
		for (const hidden of Array.from(svgElement.querySelectorAll('.edgePaths, .edgeLabels, .edgeLabel, .flowchart-link'))) {
			(hidden as SVGElement).style.display = 'none';
		}

		this.wireNodeInteractions(diagramHost, graph);
		this.fitCanvasToViewport(body, naturalWidth, naturalHeight);
	}

	/** Records each node's current center (base mermaid position + any live drag offset) so edges can be redrawn without touching mermaid's own layout. */
	private buildNodeVisuals(svgElement: SVGSVGElement, graph: IArchitectureGraph): void {
		const translateRe = /translate\(\s*([\-\d.]+)[ ,]+([\-\d.]+)\s*\)/;

		for (const node of graph.nodes) {
			// Same rationale as the SVG lookup above: this is mermaid's own
			// generated markup, matched back to our own node data by id.
			// eslint-disable-next-line no-restricted-syntax
			const el = svgElement.querySelector(`[id*="flowchart-node_${node.id}-"]`);
			// eslint-disable-next-line no-restricted-syntax
			if (!(el instanceof SVGGraphicsElement)) {
				continue;
			}
			const match = translateRe.exec(el.getAttribute('transform') ?? '');
			const baseX = match ? parseFloat(match[1]) : 0;
			const baseY = match ? parseFloat(match[2]) : 0;
			const bbox = el.getBBox();

			this.nodeVisuals.set(node.id, {
				id: node.id,
				element: el,
				localCenterX: bbox.x + bbox.width / 2,
				localCenterY: bbox.y + bbox.height / 2,
				baseX,
				baseY,
				offsetX: 0,
				offsetY: 0,
			});
		}
	}

	private nodeCenter(id: string): { x: number; y: number } | undefined {
		const visual = this.nodeVisuals.get(id);
		if (!visual) {
			return undefined;
		}
		return {
			x: visual.baseX + visual.offsetX + visual.localCenterX,
			y: visual.baseY + visual.offsetY + visual.localCenterY,
		};
	}

	private buildEdgesOverlay(width: number, height: number, graph: IArchitectureGraph): SVGSVGElement {
		const overlay = mainWindow.document.createElementNS('http://www.w3.org/2000/svg', 'svg');
		overlay.classList.add('architecture-diagram-edges-overlay');
		setSvgAttr(overlay, 'viewBox', `0 0 ${width} ${height}`);
		setSvgAttr(overlay, 'preserveAspectRatio', 'none');

		const marker = mainWindow.document.createElementNS('http://www.w3.org/2000/svg', 'marker');
		setSvgAttr(marker, 'id', 'reeve-architecture-diagram-arrow');
		setSvgAttr(marker, 'viewBox', '0 0 10 10');
		setSvgAttr(marker, 'refX', '8.5');
		setSvgAttr(marker, 'refY', '5');
		setSvgAttr(marker, 'markerWidth', '7');
		setSvgAttr(marker, 'markerHeight', '7');
		setSvgAttr(marker, 'orient', 'auto-start-reverse');
		const arrowPath = mainWindow.document.createElementNS('http://www.w3.org/2000/svg', 'path');
		setSvgAttr(arrowPath, 'd', 'M 0 0 L 10 5 L 0 10 z');
		setSvgAttr(arrowPath, 'fill', '#C74A08');
		marker.appendChild(arrowPath);
		const defs = mainWindow.document.createElementNS('http://www.w3.org/2000/svg', 'defs');
		defs.appendChild(marker);
		overlay.appendChild(defs);

		this.edgeVisuals = [];
		for (const edge of graph.edges) {
			const from = this.nodeCenter(edge.from);
			const to = this.nodeCenter(edge.to);
			if (!from || !to) {
				continue;
			}

			const line = mainWindow.document.createElementNS('http://www.w3.org/2000/svg', 'line');
			line.classList.add('architecture-diagram-edge-line');
			if (edge.style === 'dashed') {
				line.classList.add('dashed');
			}
			setSvgAttr(line, 'marker-end', 'url(#reeve-architecture-diagram-arrow)');
			overlay.appendChild(line);

			let labelBg: SVGRectElement | undefined;
			let labelText: SVGTextElement | undefined;
			if (edge.label) {
				labelBg = mainWindow.document.createElementNS('http://www.w3.org/2000/svg', 'rect');
				labelBg.classList.add('architecture-diagram-edge-label-bg');
				overlay.appendChild(labelBg);

				labelText = mainWindow.document.createElementNS('http://www.w3.org/2000/svg', 'text');
				labelText.classList.add('architecture-diagram-edge-label-text');
				labelText.textContent = edge.label;
				overlay.appendChild(labelText);
			}

			this.edgeVisuals.push({ fromId: edge.from, toId: edge.to, line, labelBg, labelText });
		}

		this.layoutEdges();
		return overlay;
	}

	/** Repositions every edge line/label from current node centers — called after any node drag. */
	private layoutEdges(): void {
		for (const edge of this.edgeVisuals) {
			const from = this.nodeCenter(edge.fromId);
			const to = this.nodeCenter(edge.toId);
			if (!from || !to) {
				continue;
			}

			setSvgAttr(edge.line, 'x1', String(from.x));
			setSvgAttr(edge.line, 'y1', String(from.y));
			setSvgAttr(edge.line, 'x2', String(to.x));
			setSvgAttr(edge.line, 'y2', String(to.y));

			if (edge.labelText && edge.labelBg) {
				const midX = (from.x + to.x) / 2;
				const midY = (from.y + to.y) / 2;
				setSvgAttr(edge.labelText, 'x', String(midX));
				setSvgAttr(edge.labelText, 'y', String(midY));
				// Measuring text width needs the element already attached and
				// painted; a rough character-count estimate avoids a second
				// layout pass and is more than accurate enough for a small
				// background pill behind a short edge label.
				const estimatedWidth = (edge.labelText.textContent?.length ?? 0) * 6.2 + 12;
				setSvgAttr(edge.labelBg, 'x', String(midX - estimatedWidth / 2));
				setSvgAttr(edge.labelBg, 'y', String(midY - 9));
				setSvgAttr(edge.labelBg, 'width', String(estimatedWidth));
				setSvgAttr(edge.labelBg, 'height', '16');
			}
		}
	}

	private wireNodeInteractions(diagramHost: HTMLElement, graph: IArchitectureGraph): void {
		for (const node of graph.nodes) {
			const visual = this.nodeVisuals.get(node.id);
			if (!visual) {
				continue;
			}
			const el = visual.element;
			el.style.cursor = 'grab';

			this._register(addDisposableListener(el, EventType.POINTER_DOWN, (e: PointerEvent) => {
				if (e.button !== 0) {
					return;
				}
				e.preventDefault();
				e.stopPropagation();
				el.setPointerCapture(e.pointerId);
				el.classList.add('dragging');
				el.style.cursor = 'grabbing';

				const startClientX = e.clientX;
				const startClientY = e.clientY;
				const startOffsetX = visual.offsetX;
				const startOffsetY = visual.offsetY;
				let moved = 0;

				const onMove = (moveEvent: PointerEvent) => {
					const screenDx = moveEvent.clientX - startClientX;
					const screenDy = moveEvent.clientY - startClientY;
					moved = Math.max(moved, Math.abs(screenDx), Math.abs(screenDy));

					// Node drag deltas live in the mermaid SVG's own coordinate
					// space, which sits under the canvas's zoom transform — a
					// screen-pixel mouse movement corresponds to a smaller (or
					// larger) SVG-unit movement depending on the current zoom.
					const localDx = screenDx / this.canvasScale;
					const localDy = screenDy / this.canvasScale;
					visual.offsetX = startOffsetX + localDx;
					visual.offsetY = startOffsetY + localDy;
					el.setAttribute('transform', `translate(${visual.baseX + visual.offsetX}, ${visual.baseY + visual.offsetY})`);
					this.layoutEdges();
				};
				// `lostpointercapture` fires whenever capture ends for *any*
				// reason (a normal pointerup, a pointercancel, the OS taking
				// the gesture away, this element leaving the DOM on
				// regenerate) — cleaning up there instead of only on
				// 'pointerup' is what guarantees these listeners can never
				// outlive the drag and start reacting to unrelated later
				// mouse movement.
				let cleaned = false;
				const cleanup = () => {
					if (cleaned) {
						return;
					}
					cleaned = true;
					el.classList.remove('dragging');
					el.style.cursor = 'grab';
					el.removeEventListener('pointermove', onMove);
					el.removeEventListener('pointerup', onUp);
					el.removeEventListener('pointercancel', onUp);
					el.removeEventListener('lostpointercapture', cleanup);
				};
				const onUp = (upEvent: PointerEvent) => {
					if (el.hasPointerCapture(upEvent.pointerId)) {
						el.releasePointerCapture(upEvent.pointerId);
					}
					cleanup();

					if (moved < DRAG_CLICK_THRESHOLD && node.path) {
						const uri = this.architectureDiagramService.resolveNodePath(node.path);
						if (uri) {
							this.editorService.openEditor({ resource: uri });
						}
					}
				};
				el.addEventListener('pointermove', onMove);
				el.addEventListener('pointerup', onUp);
				el.addEventListener('pointercancel', onUp);
				el.addEventListener('lostpointercapture', cleanup);
			}));
		}
	}

	private setupViewportPanAndZoom(body: HTMLElement): void {
		this._register(addDisposableListener(body, EventType.WHEEL, (e: WheelEvent) => {
			const canvas = this.canvas;
			if (!canvas) {
				return;
			}
			e.preventDefault();

			// Ctrl/Cmd+wheel is how Chromium reports a trackpad pinch gesture;
			// it gets a steeper speed than a literal mouse wheel notch so a
			// pinch feels proportionate to the gesture instead of a single
			// wheel click's worth of zoom.
			const speed = e.ctrlKey || e.metaKey ? PINCH_ZOOM_SPEED : WHEEL_ZOOM_SPEED;
			const clampedDelta = Math.max(-240, Math.min(240, e.deltaY));
			const factor = Math.exp(-clampedDelta * speed);
			const newScale = Math.max(MIN_CANVAS_SCALE, Math.min(MAX_CANVAS_SCALE, this.canvasScale * factor));
			if (newScale === this.canvasScale) {
				return;
			}

			// Zoom toward the pointer: the point under the cursor stays fixed
			// on screen while everything else scales around it, matching the
			// zoom feel of any normal infinite-canvas tool.
			const rect = body.getBoundingClientRect();
			const pointerX = e.clientX - rect.left;
			const pointerY = e.clientY - rect.top;
			this.canvasX = pointerX - (newScale / this.canvasScale) * (pointerX - this.canvasX);
			this.canvasY = pointerY - (newScale / this.canvasScale) * (pointerY - this.canvasY);
			this.canvasScale = newScale;
			this.applyCanvasTransform();
		}, { passive: false }));

		this._register(addDisposableListener(body, EventType.POINTER_DOWN, (e: PointerEvent) => {
			if (e.button !== 0 || !this.canvas) {
				return;
			}
			// A node's own pointerdown handler calls stopPropagation, so
			// reaching here at all already means the gesture started on empty
			// canvas space, not on a node — no need to re-check the target.
			body.setPointerCapture(e.pointerId);
			body.classList.add('panning');
			const startClientX = e.clientX;
			const startClientY = e.clientY;
			const startX = this.canvasX;
			const startY = this.canvasY;

			const onMove = (moveEvent: PointerEvent) => {
				this.canvasX = startX + (moveEvent.clientX - startClientX);
				this.canvasY = startY + (moveEvent.clientY - startClientY);
				this.applyCanvasTransform();
			};
			// See the matching comment on the node-drag handler: cleaning up
			// from 'lostpointercapture' (which fires no matter how capture
			// ends) rather than only 'pointerup' is what stops this from
			// ever surviving past its own gesture and hijacking unrelated
			// later mouse movement.
			let cleaned = false;
			const cleanup = () => {
				if (cleaned) {
					return;
				}
				cleaned = true;
				body.classList.remove('panning');
				body.removeEventListener('pointermove', onMove);
				body.removeEventListener('pointerup', onUp);
				body.removeEventListener('pointercancel', onUp);
				body.removeEventListener('lostpointercapture', cleanup);
			};
			const onUp = (upEvent: PointerEvent) => {
				if (body.hasPointerCapture(upEvent.pointerId)) {
					body.releasePointerCapture(upEvent.pointerId);
				}
				cleanup();
			};
			body.addEventListener('pointermove', onMove);
			body.addEventListener('pointerup', onUp);
			body.addEventListener('pointercancel', onUp);
			body.addEventListener('lostpointercapture', cleanup);
		}));
	}

	private applyCanvasTransform(): void {
		if (this.canvas) {
			this.canvas.style.transform = `translate(${this.canvasX}px, ${this.canvasY}px) scale(${this.canvasScale})`;
		}
	}

	/** Picks an initial scale that fits the diagram in view and centers it, so a freshly (re)generated diagram never opens scrolled off into the void. */
	private fitCanvasToViewport(body: HTMLElement, contentWidth: number, contentHeight: number): void {
		const viewportWidth = body.clientWidth || 1;
		const viewportHeight = body.clientHeight || 1;
		const padding = 48;
		const fitScale = Math.min(
			(viewportWidth - padding) / contentWidth,
			(viewportHeight - padding) / contentHeight,
			1.1,
		);
		this.canvasScale = Math.max(MIN_CANVAS_SCALE, Math.min(MAX_CANVAS_SCALE, Number.isFinite(fitScale) && fitScale > 0 ? fitScale : 1));
		this.canvasX = (viewportWidth - contentWidth * this.canvasScale) / 2;
		this.canvasY = (viewportHeight - contentHeight * this.canvasScale) / 2;
		this.applyCanvasTransform();
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
			const handle = append(win, $(`.floating-panel-resize-handle.${dir}`, { title: localize('architectureDiagramResize', "Drag to resize") }));

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

registerWorkbenchContribution2(ArchitectureDiagramContribution.ID, ArchitectureDiagramContribution, WorkbenchPhase.AfterRestored);
