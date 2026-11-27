// Единый список исключений vault: его читают и check-vault, и astro.config.mjs (starlight-obsidian `ignore`).
// Точки-каталоги и node_modules — на любой глубине; docs/ и README.md — только в корне vault.
// Labs/: страницы лаб — заметки, а подкаталоги (код стендов с README, docs и т. п.) в vault не входят.
export const LABS_DIR = 'Labs';
export const STARLIGHT_IGNORE = ['.*/**', '**/.*/**', 'docs/**', 'README.md', '**/node_modules/**', `${LABS_DIR}/*/**`];

const ANY_DEPTH_DIRS = new Set(['node_modules']);
const ROOT_DIRS = new Set(['docs']);
const ROOT_FILES = new Set(['README.md']);

// parent — путь каталога-родителя относительно корня vault ('' в корне).
export const isIgnoredDir = (name, atRoot, parent = '') =>
  name.startsWith('.') || ANY_DEPTH_DIRS.has(name) || (atRoot && ROOT_DIRS.has(name)) || parent === LABS_DIR;

export const isIgnoredFile = (name, atRoot) => atRoot && ROOT_FILES.has(name);
