// Sätteri hast-плагин: строка ссылок с меткой («Рядом: [[A]] · [[B]] · [[C]]», «- **Язык:** [[A]] · [[B]]»)
// на сайте выводится столбиком. В заметках остаётся строкой.
//
// Строка — абзац целиком или его часть до мягкого переноса. Метка — «Слово:» или <strong>Слово:</strong> в её начале.
// Пункты разделены « · » (вне скобок). Метки «Рядом», «Разделы», «Страницы» — свои: хватит двух пунктов,
// пункт может быть без ссылки, а вместо « · » допустимы запятые, если каждый пункт — ссылка с пояснением
// в скобках или без. Любая другая метка — от трёх пунктов, ссылки во всех, кроме, может быть, одного:
// так не задеваются строки вида «T: O(log n · C) · S: O(1)» и «[задача](url) · Medium · нюанс».

const DOT = /^\s+·\s+/;
const COMMA = /^,\s+/;
const TEXT_LABEL = /^\s*([^:·/\n]{1,30}:)(?!\/)\s*/;
const KNOWN_LABELS = new Set(['Рядом:', 'Разделы:', 'Страницы:']);
const MIN_ITEMS = 3;
const MIN_KNOWN_ITEMS = 2;

const isText = (n) => n?.type === 'text';
const isElement = (n, tag) => n?.type === 'element' && (!tag || n.tagName === tag);
const textOf = (n) => (isText(n) ? n.value : (n?.children ?? []).map(textOf).join(''));
const hasLink = (nodes) => nodes.some((n) => isElement(n, 'a') || (n?.children && hasLink(n.children)));

const text = (value) => ({ type: 'text', value });
const element = (tagName, children, className) => ({
  type: 'element',
  tagName,
  properties: className ? { className: [className] } : {},
  children,
});

// Режет содержимое по переводам строки в текстовых узлах верхнего уровня (мягкий перенос внутри абзаца).
function splitLines(children) {
  const lines = [[]];
  for (const n of children) {
    if (!isText(n) || !n.value.includes('\n')) {
      lines.at(-1).push(n);
      continue;
    }
    n.value.split('\n').forEach((piece, i) => {
      if (i > 0) lines.push([]);
      if (piece) lines.at(-1).push(text(piece));
    });
  }
  return lines;
}

// Метка и остаток строки либо null, если метки нет.
function splitLabel(line) {
  const [first, ...rest] = line;
  if (isText(first)) {
    const m = first.value.match(TEXT_LABEL);
    if (!m) return null;
    const tail = first.value.slice(m[0].length);
    return { label: [text(m[1])], body: tail ? [text(tail), ...rest] : rest };
  }
  if (isElement(first, 'strong')) {
    const label = textOf(first).trim();
    if (label.length > 31 || !label.endsWith(':')) return null;
    return { label: [first], body: rest };
  }
  return null;
}

// Режет строку на пункты по разделителю в текстовых узлах верхнего уровня, вне круглых скобок.
function splitItems(body, separator) {
  const items = [[]];
  let depth = 0;
  for (const n of body) {
    if (!isText(n)) {
      items.at(-1).push(n);
      continue;
    }
    let buf = '';
    const flush = () => {
      if (buf) items.at(-1).push(text(buf));
      buf = '';
    };
    for (let i = 0; i < n.value.length; ) {
      const ch = n.value[i];
      const sep = depth === 0 && n.value.slice(i).match(separator);
      if (sep) {
        flush();
        items.push([]);
        i += sep[0].length;
        continue;
      }
      if (ch === '(') depth += 1;
      else if (ch === ')') depth = Math.max(0, depth - 1);
      buf += ch;
      i += 1;
    }
    flush();
  }
  return items.map(trimItem);
}

// Пункт «через запятую»: ровно одна ссылка и, может быть, одно пояснение в скобках после неё.
function isLinkWithNote(item) {
  const [first, ...rest] = item;
  if (!isElement(first, 'a') || hasLink(rest)) return false;
  const note = rest.map(textOf).join('').trim().replace(/\.$/, '').trim();
  if (!note) return true;
  if (!note.startsWith('(') || !note.endsWith(')')) return false;
  let depth = 0;
  for (let i = 0; i < note.length; i += 1) {
    if (note[i] === '(') depth += 1;
    else if (note[i] === ')') depth -= 1;
    if (depth === 0 && i < note.length - 1) return false; // скобка закрылась раньше конца — это не одно пояснение
  }
  return depth === 0;
}

function trimItem(nodes) {
  const out = [...nodes];
  if (isText(out[0])) out[0] = text(out[0].value.trimStart());
  if (isText(out.at(-1))) out[out.length - 1] = text(out.at(-1).value.trimEnd());
  return out.filter((n) => !isText(n) || n.value);
}

// Точка в конце строки — конец предложения, если стоит сразу после ссылки или скобки; иначе она часть пункта («и т.д.»).
function dropTrailingDot(items) {
  const last = items.at(-1);
  const tail = last.at(-1);
  if (!isText(tail) || !tail.value.endsWith('.')) return;
  const rest = tail.value.slice(0, -1).trimEnd();
  const afterLink = !rest && isElement(last.at(-2), 'a');
  if (!afterLink && !rest.endsWith(')')) return;
  if (rest) last[last.length - 1] = text(rest);
  else last.pop();
}

// Пункты строки по правилам из шапки файла либо null.
function itemsOf(label, body) {
  const known = KNOWN_LABELS.has(textOf({ children: label }).trim());
  const dotted = body.some((n) => isText(n) && /\s·\s/.test(n.value));
  if (!dotted && !known) return null;
  const items = splitItems(body, dotted ? DOT : COMMA);
  if (items.some((it) => it.length === 0)) return null;
  if (!dotted) return items.length >= MIN_KNOWN_ITEMS && items.every(isLinkWithNote) ? items : null;
  if (known) return items.length >= MIN_KNOWN_ITEMS ? items : null;
  if (items.length < MIN_ITEMS || items.filter(hasLink).length < items.length - 1) return null;
  return items;
}

// Метка и ul для одной строки либо null, если строка — не список ссылок.
function lineToList(line, ulClass) {
  const parts = splitLabel(line);
  if (!parts) return null;
  const items = itemsOf(parts.label, parts.body);
  if (!items) return null;
  dropTrailingDot(items);
  return { label: parts.label, ul: element('ul', items.map((it) => element('li', it)), ulClass) };
}

// Строки абзаца обратно в узлы: обычные строки подряд — один <p> с переносами, строка-список — div.link-list.
function paragraphToNodes(node) {
  const out = [];
  let plain = [];
  let changed = false;
  const flush = () => {
    if (plain.length) out.push({ ...node, children: plain });
    plain = [];
  };
  for (const line of splitLines(node.children ?? [])) {
    const list = lineToList(line);
    if (!list) {
      if (plain.length) plain.push(text('\n'));
      plain.push(...line);
      continue;
    }
    changed = true;
    flush();
    out.push(element('div', [element('p', list.label, 'link-list-label'), list.ul], 'link-list'));
  }
  flush();
  return changed ? out : null;
}

// Пункт списка: меняется только первая строка (до вложенного списка), остальное сохраняется.
// Текстовый узел с первым переводом строки делится: до него — в строку, с него — в хвост.
function listItemToNodes(node) {
  const children = node.children ?? [];
  const br = children.findIndex((n) => isText(n) && n.value.includes('\n'));
  let head = children;
  let tail = [];
  if (br !== -1) {
    const { value } = children[br];
    const cut = value.indexOf('\n');
    head = [...children.slice(0, br), ...(cut > 0 ? [text(value.slice(0, cut))] : [])];
    tail = [text(value.slice(cut)), ...children.slice(br + 1)];
  }
  const list = lineToList(trimItem(head), 'link-list');
  if (!list) return null;
  return [{ ...node, children: [...list.label, list.ul, ...tail] }];
}

// Узлы на замену p или li либо null, если менять нечего.
export function toLinkLists(node) {
  return node.tagName === 'li' ? listItemToNodes(node) : paragraphToNodes(node);
}

export default function rehypeLinkLists() {
  return {
    name: 'link-lists',
    element: {
      filter: ['p', 'li'],
      visit(node, ctx) {
        const out = toLinkLists(node);
        if (!out) return;
        if (node.tagName === 'li') ctx.setField(node, 'children', out[0].children);
        else ctx.replaceNode(node, out);
      },
    },
  };
}
