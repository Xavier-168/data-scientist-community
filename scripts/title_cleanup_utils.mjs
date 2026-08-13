function normalizeTitleText(value) {
  return String(value ?? '')
    .replace(/\n+/g, ' ')
    .replace(/[\u200B-\u200D\uFEFF\u00A0]+/gu, ' ')
    .replace(/＃/g, '#')
    .replace(/\s*#.*$/u, '')
    .replace(/\s+/g, ' ')
    .trim();
}

function compactEpisodeTitle(text) {
  if (!text || !/\s/.test(text)) return text;
  const hasEpisodeMarker = /[（(【\[]\s*[上中下终]\s*[）)】\]]|(?:^|[?？!！。,.，、;；:：])\s*[上下中终](?:集|期|篇)?/u.test(text);
  const hasBoilerplateTail = /用自己的项目测试了|详细信息可以参考上期视频|希望本期视频内容可以帮助到你|数据科学家的详细信息/u.test(text);
  if (!hasEpisodeMarker || !hasBoilerplateTail) return text;

  const bracketed = text.match(/^(.*?[（(【\[]\s*[上中下终]\s*[）)】\]](?:集|期|篇)?)(?=\s+)/u);
  if (bracketed?.[1]) return bracketed[1].trim();

  const plainMarker = text.match(/^(.*?[上下中终](?:集|期|篇)?)(?=\s+)/u);
  if (plainMarker?.[1]) return plainMarker[1].trim();

  return text;
}

export function cleanCollectedTitle(value) {
  return compactEpisodeTitle(normalizeTitleText(value));
}
