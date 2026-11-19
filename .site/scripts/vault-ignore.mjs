// Единый список исключений vault: его читают и check-vault, и astro.config.mjs (starlight-obsidian `ignore`).
// Точки-каталоги и node_modules — на любой глубине; docs/ и README.md — только в корне vault.
export const STARLIGHT_IGNORE = ['.*/**', '**/.*/**', 'docs/**', 'README.md', '**/node_modules/**'];

const ANY_DEPTH_DIRS = new Set(['node_modules']);
const ROOT_DIRS = new Set(['docs']);
const ROOT_FILES = new Set(['README.md']);

export const isIgnoredDir = (name, atRoot) =>
  name.startsWith('.') || ANY_DEPTH_DIRS.has(name) || (atRoot && ROOT_DIRS.has(name));

export const isIgnoredFile = (name, atRoot) => atRoot && ROOT_FILES.has(name);
