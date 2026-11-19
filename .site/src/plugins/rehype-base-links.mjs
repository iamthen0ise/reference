// Sätteri hast-плагин (аналог rehype): добавляет Astro `base` к корневым ссылкам <a href="/...">.
// starlight-obsidian пишет ссылки вида /notes/..., не учитывая base.
export default function rehypeBaseLinks({ base = '' } = {}) {
  const prefix = base.replace(/\/+$/, '');
  return {
    name: 'base-links',
    element: {
      filter: ['a'],
      visit(node, ctx) {
        const href = node.properties?.href;
        if (
          prefix &&
          typeof href === 'string' &&
          href.startsWith('/') &&
          !href.startsWith('//') &&
          href !== prefix &&
          !href.startsWith(prefix + '/')
        ) {
          ctx.setProperty(node, 'href', prefix + href);
        }
      },
    },
  };
}
