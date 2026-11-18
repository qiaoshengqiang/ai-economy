/* AIEOS Markdown —— 无依赖、无原始 HTML 的安全子集渲染器。
 *
 * 🔒 只生成固定标签：标题 / 段落 / 列表 / 表格 / 代码 / 引用 / 链接。
 *    所有文本先 escape，链接只允许 http(s)/mailto。
 *    不生成 script / iframe / style / img / svg / 事件属性。
 */
(() => {
  "use strict";

  const ALLOWED_LINK = /^(https?:\/\/|mailto:)/i;

  function escapeHtml(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function safeUrl(value) {
    const url = String(value == null ? "" : value).trim();
    if (!url || !ALLOWED_LINK.test(url)) return "";
    return url
      .replace(/"/g, "%22")
      .replace(/</g, "%3C")
      .replace(/>/g, "%3E");
  }

  function inline(text) {
    let out = escapeHtml(text);
    const code = [];
    // 行内代码先占位，避免内部内容被当成粗体/链接。
    out = out.replace(/`([^`]+)`/g, (_match, body) => {
      const token = `\u0000CODE${code.length}\u0000`;
      code.push(`<code>${body}</code>`);
      return token;
    });
    // 图片暂不加载外部资源，降级成链接。
    out = out.replace(
      /!\[([^\]]*)\]\(([^)\s]+)\)/g,
      (_match, alt, url) => {
        const href = safeUrl(url);
        const label = alt ? `图片：${alt}` : "图片";
        return href
          ? `<a href="${href}" target="_blank" rel="noopener noreferrer">${label}</a>`
          : label;
      });
    out = out.replace(
      /\[([^\]]+)\]\(([^)\s]+)\)/g,
      (_match, label, url) => {
        const href = safeUrl(url);
        return href
          ? `<a href="${href}" target="_blank" rel="noopener noreferrer">${label}</a>`
          : label;
      });
    out = out.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    out = out.replace(/__([^_]+)__/g, "<strong>$1</strong>");
    out = out.replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>");
    out = out.replace(/(^|[^_])_([^_\n]+)_/g, "$1<em>$2</em>");
    out = out.replace(/~~([^~]+)~~/g, "<del>$1</del>");
    out = out.replace(
      /\u0000CODE(\d+)\u0000/g,
      (_match, index) => code[Number(index)] || "");
    return out;
  }

  function isTableSeparator(line) {
    const value = String(line || "").trim();
    if (!value.includes("-")) return false;
    return /^\|?[\s:|-]+\|?$/.test(value);
  }

  function isTableStart(lines, index) {
    if (index + 1 >= lines.length) return false;
    const header = String(lines[index] || "").trim();
    if (!header.includes("|")) return false;
    return isTableSeparator(lines[index + 1]);
  }

  function splitRow(line) {
    return String(line || "")
      .trim()
      .replace(/^\|/, "")
      .replace(/\|$/, "")
      .split("|")
      .map((cell) => cell.trim());
  }

  function parseTable(lines, index) {
    const headers = splitRow(lines[index]);
    const aligns = splitRow(lines[index + 1]).map((cell) => {
      const left = cell.startsWith(":");
      const right = cell.endsWith(":");
      if (left && right) return "center";
      if (right) return "right";
      return "left";
    });
    let cursor = index + 2;
    const rows = [];
    while (cursor < lines.length
      && String(lines[cursor] || "").trim()
      && String(lines[cursor]).includes("|")) {
      rows.push(splitRow(lines[cursor]));
      cursor += 1;
    }
    const head = headers.map((cell, cellIndex) => {
      const align = aligns[cellIndex] || "left";
      return `<th class="md-${align}">${inline(cell)}</th>`;
    }).join("");
    const body = rows.map((row) => {
      const cells = headers.map((_header, cellIndex) => {
        const align = aligns[cellIndex] || "left";
        return `<td class="md-${align}">${inline(row[cellIndex] || "")}</td>`;
      }).join("");
      return `<tr>${cells}</tr>`;
    }).join("");
    return {
      html: `<table class="md-table"><thead><tr>${head}</tr></thead>`
        + `<tbody>${body}</tbody></table>`,
      next: cursor,
    };
  }

  function isListLine(line) {
    return /^\s*(?:[-*+]|\d+\.)\s+/.test(String(line || ""));
  }

  function parseList(lines, index) {
    const ordered = /^\s*\d+\.\s+/.test(lines[index]);
    const items = [];
    let cursor = index;
    while (cursor < lines.length) {
      const match = String(lines[cursor] || "").match(
        /^\s*(?:[-*+]|\d+\.)\s+(.*)$/);
      if (!match) break;
      let text = match[1];
      cursor += 1;
      // 续行：缩进 2 格以上、且不是新的列表项。
      while (cursor < lines.length
        && String(lines[cursor] || "").trim()
        && !isListLine(lines[cursor])
        && /^\s{2,}\S/.test(lines[cursor])) {
        text += "\n" + String(lines[cursor]).trim();
        cursor += 1;
      }
      items.push(text);
    }
    const tag = ordered ? "ol" : "ul";
    const body = items
      .map((item) => `<li>${inline(item)}</li>`)
      .join("");
    return { html: `<${tag} class="md-list">${body}</${tag}>`, next: cursor };
  }

  function isBlockStart(line) {
    const value = String(line || "");
    return /^\s*```/.test(value)
      || /^(#{1,6})\s+/.test(value)
      || /^\s*>/.test(value)
      || isListLine(value)
      || /^\s*(?:---+|\*\*\*+)\s*$/.test(value)
      || /^\s*\|.*\|\s*$/.test(value);
  }

  function render(text) {
    const lines = String(text == null ? "" : text)
      .replace(/\r\n?/g, "\n")
      .split("\n");
    const out = [];
    let index = 0;
    while (index < lines.length) {
      const line = lines[index];
      if (!String(line || "").trim()) {
        index += 1;
        continue;
      }

      const fence = String(line).match(
        /^\s*```([A-Za-z0-9_+-]*)\s*$/);
      if (fence) {
        const language = fence[1] || "";
        const body = [];
        index += 1;
        while (index < lines.length
          && !/^\s*```\s*$/.test(lines[index])) {
          body.push(lines[index]);
          index += 1;
        }
        if (index < lines.length) index += 1;
        const languageClass = language
          ? ` class="language-${escapeHtml(language)}"`
          : "";
        out.push(
          `<pre class="md-code"><code${languageClass}>`
          + `${escapeHtml(body.join("\n"))}</code></pre>`);
        continue;
      }

      const heading = String(line).match(/^(#{1,6})\s+(.*)$/);
      if (heading) {
        const level = heading[1].length;
        out.push(`<h${level}>${inline(heading[2].trim())}</h${level}>`);
        index += 1;
        continue;
      }

      if (/^\s*(?:---+|\*\*\*+)\s*$/.test(String(line))) {
        out.push("<hr>");
        index += 1;
        continue;
      }

      if (/^\s*>\s?/.test(String(line))) {
        const body = [];
        while (index < lines.length
          && /^\s*>\s?/.test(String(lines[index]))) {
          body.push(String(lines[index]).replace(/^\s*>\s?/, ""));
          index += 1;
        }
        out.push(
          `<blockquote>${body.map(inline).join("<br>")}</blockquote>`);
        continue;
      }

      if (isTableStart(lines, index)) {
        const table = parseTable(lines, index);
        out.push(table.html);
        index = table.next;
        continue;
      }

      if (isListLine(line)) {
        const list = parseList(lines, index);
        out.push(list.html);
        index = list.next;
        continue;
      }

      const paragraph = [];
      while (index < lines.length
        && String(lines[index] || "").trim()
        && !isBlockStart(lines[index])) {
        paragraph.push(String(lines[index]));
        index += 1;
      }
      if (paragraph.length) {
        out.push(`<p>${paragraph.map(inline).join("<br>")}</p>`);
      } else {
        out.push(`<p>${inline(line)}</p>`);
        index += 1;
      }
    }
    return out.join("\n");
  }

  window.AIEOSMarkdown = { render, escapeHtml };
})();
