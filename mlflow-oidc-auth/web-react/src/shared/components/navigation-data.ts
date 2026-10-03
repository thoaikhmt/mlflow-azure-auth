import type { IconDefinition } from "@fortawesome/fontawesome-svg-core";

export type NavLinkData = {
  label: string;
  href: string;
  isInternalLink?: boolean;
  icon?: IconDefinition;
};

export type NavigationData = {
  mainLinks: NavLinkData[];
  userControls: NavLinkData[];
};

export const getNavigationData = (
  userName: string,
  basePath: string,
): NavigationData => ({
  mainLinks: [{ label: "MLFlow", href: `${basePath}/` }],
  userControls: [
    { label: `Hello, ${userName}`, href: "/user", isInternalLink: true },
    { label: "Logout", href: `${basePath}/logout` },
  ],
});
