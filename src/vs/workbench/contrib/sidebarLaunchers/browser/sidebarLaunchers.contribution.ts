/*---------------------------------------------------------------------------------------------
 *  Copyright (c) Microsoft Corporation. All rights reserved.
 *  Licensed under the MIT License. See License.txt in the project root for license information.
 *--------------------------------------------------------------------------------------------*/

import { localize } from '../../../../nls.js';
import { Codicon } from '../../../../base/common/codicons.js';
import { SyncDescriptor } from '../../../../platform/instantiation/common/descriptors.js';
import { registerIcon } from '../../../../platform/theme/common/iconRegistry.js';
import { Registry } from '../../../../platform/registry/common/platform.js';
import { IViewsRegistry, IViewDescriptor, Extensions as ViewExtensions } from '../../../common/views.js';
import { VIEW_CONTAINER } from '../../files/browser/explorerViewlet.js';
import { SidebarLaunchersPane } from './sidebarLaunchersPane.js';

// Styling for this pane's button row lives in floatingPanels.css (appended
// there rather than a new CSS module) — see the note in that file for why.

const sidebarLaunchersViewIcon = registerIcon('sidebar-launchers-view-icon', Codicon.layout, localize('sidebarLaunchersViewIcon', 'View icon of the quick views launcher.'));

export const SidebarLaunchersPaneId = 'workbench.explorer.sidebarLaunchersView';

class SidebarLaunchersPaneDescriptor implements IViewDescriptor {
	readonly id = SidebarLaunchersPaneId;
	readonly name = SidebarLaunchersPane.TITLE;
	readonly containerIcon = sidebarLaunchersViewIcon;
	readonly ctorDescriptor = new SyncDescriptor(SidebarLaunchersPane);
	// Between the file tree (ExplorerView, order: 1) and Outline (order: 2) — a
	// fractional value avoids tying with either and depending on registration
	// order to break the tie.
	readonly order = 1.5;
	readonly weight = 30;
	readonly collapsed = false;
	readonly canToggleVisibility = true;
	readonly hideByDefault = false;
	readonly canMoveView = true;
}

Registry.as<IViewsRegistry>(ViewExtensions.ViewsRegistry).registerViews([new SidebarLaunchersPaneDescriptor()], VIEW_CONTAINER);
