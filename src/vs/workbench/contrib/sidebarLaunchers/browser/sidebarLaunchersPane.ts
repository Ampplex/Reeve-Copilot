/*---------------------------------------------------------------------------------------------
 *  Copyright (c) Microsoft Corporation. All rights reserved.
 *  Licensed under the MIT License. See License.txt in the project root for license information.
 *--------------------------------------------------------------------------------------------*/

import { $, append, addDisposableListener, EventType } from '../../../../base/browser/dom.js';
import { localize, localize2 } from '../../../../nls.js';
import { ILocalizedString } from '../../../../platform/action/common/action.js';
import { IContextKeyService } from '../../../../platform/contextkey/common/contextkey.js';
import { IContextMenuService } from '../../../../platform/contextview/browser/contextView.js';
import { IInstantiationService } from '../../../../platform/instantiation/common/instantiation.js';
import { IKeybindingService } from '../../../../platform/keybinding/common/keybinding.js';
import { IConfigurationService } from '../../../../platform/configuration/common/configuration.js';
import { IOpenerService } from '../../../../platform/opener/common/opener.js';
import { IThemeService } from '../../../../platform/theme/common/themeService.js';
import { IHoverService } from '../../../../platform/hover/browser/hover.js';
import { INotificationService } from '../../../../platform/notification/common/notification.js';
import { ViewPane, IViewPaneOptions } from '../../../browser/parts/views/viewPane.js';
import { IViewDescriptorService } from '../../../common/views.js';

/**
 * Small header pane holding the Timeline and Architecture Diagram launcher
 * buttons (side by side, above the Outline view). Each button is meant to
 * open its feature in a floating window — the actual target UI for each is
 * still being designed, so pressing a button currently only shows a
 * placeholder notification. Neither button touches the existing Timeline
 * view or command registrations, which remain fully intact and unrelated to
 * this pane.
 */
export class SidebarLaunchersPane extends ViewPane {
	static readonly TITLE: ILocalizedString = localize2('sidebarLaunchers', "Quick Views");

	constructor(
		options: IViewPaneOptions,
		@IKeybindingService keybindingService: IKeybindingService,
		@IContextMenuService contextMenuService: IContextMenuService,
		@IConfigurationService configurationService: IConfigurationService,
		@IContextKeyService contextKeyService: IContextKeyService,
		@IViewDescriptorService viewDescriptorService: IViewDescriptorService,
		@IInstantiationService instantiationService: IInstantiationService,
		@IOpenerService openerService: IOpenerService,
		@IThemeService themeService: IThemeService,
		@IHoverService hoverService: IHoverService,
		@INotificationService private readonly notificationService: INotificationService,
	) {
		super(options, keybindingService, contextMenuService, configurationService, contextKeyService, viewDescriptorService, instantiationService, openerService, themeService, hoverService);

		// A single row of buttons (8px padding + 32px button height + 8px
		// padding = 48px) is all this pane ever renders — without this, the
		// pane view falls back to `Pane`'s default 120px minimum body size,
		// leaving a large empty gap below the buttons since there's no
		// scrollable/growable content to fill it. Setting both min and max
		// to the same value also makes it non-resizable by drag, which is
		// correct here since there's nothing to reveal by resizing it.
		this.minimumBodySize = 48;
		this.maximumBodySize = 48;
	}

	protected override renderBody(container: HTMLElement): void {
		const row = append(container, $('.sidebar-launchers-row'));

		this.createLauncherButton(row, 'history', localize('timelineLauncher', "Timeline"), () => {
			this.notificationService.info(localize('timelineLauncherComingSoon', "Timeline floating view is coming soon."));
		});

		this.createLauncherButton(row, 'type-hierarchy', localize('architectureDiagramLauncher', "Architecture Diagram"), () => {
			this.notificationService.info(localize('architectureDiagramLauncherComingSoon', "Architecture Diagram floating view is coming soon."));
		});
	}

	private createLauncherButton(parent: HTMLElement, codicon: string, label: string, onClick: () => void): void {
		const button = append(parent, $(`button.sidebar-launcher-button.codicon.codicon-${codicon}`, { title: label, 'aria-label': label }));
		this._register(addDisposableListener(button, EventType.CLICK, onClick));
	}

	protected override layoutBody(height: number, width: number): void {
		// Fixed-height button row; nothing to relayout.
	}
}
