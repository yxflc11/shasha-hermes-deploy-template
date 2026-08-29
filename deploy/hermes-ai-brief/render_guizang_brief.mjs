#!/usr/bin/env node
/** Deterministic, no-network renderer based on the vendored Guizang Swiss seed. */

import fs from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";

let playwright;
try {
  playwright = await import("playwright");
} catch (error) {
  const moduleRoot = (process.env.NODE_PATH || "").split(path.delimiter).find(Boolean);
  if (!moduleRoot) throw error;
  playwright = await import(pathToFileURL(path.join(moduleRoot, "playwright/index.js")).href);
}
const { chromium } = playwright.default || playwright;

const [packageArg, templateArg, outputArg] = process.argv.slice(2);
if (!packageArg || !templateArg || !outputArg) {
  throw new Error("usage: render_guizang_brief.mjs PACKAGE TEMPLATE OUTPUT_DIR");
}

const packagePath = path.resolve(packageArg);
const templatePath = path.resolve(templateArg);
const outputDir = path.resolve(outputArg);
const brief = JSON.parse(fs.readFileSync(packagePath, "utf8"));
let template = fs.readFileSync(templatePath, "utf8");
fs.mkdirSync(outputDir, { recursive: true, mode: 0o700 });

const escape = (value) => String(value ?? "")
  .replaceAll("&", "&amp;")
  .replaceAll("<", "&lt;")
  .replaceAll(">", "&gt;")
  .replaceAll('"', "&quot;")
  .replaceAll("'", "&#39;");

const dateLabel = new Intl.DateTimeFormat("zh-CN", {
  timeZone: "Asia/Shanghai", month: "2-digit", day: "2-digit",
  hour: "2-digit", minute: "2-digit", hour12: false,
}).format(new Date(brief.window.end));
const slotLabel = brief.slot === "morning" ? "昨夜 AI 大事" : "今日 AI 总结";

function imageDataUrl(asset) {
  if (!asset?.file || !asset?.mime) return "";
  let file = path.resolve(asset.file);
  if (!fs.existsSync(file)) {
    file = path.join(path.dirname(packagePath), "assets", path.basename(asset.file));
  }
  if (!fs.existsSync(file)) return "";
  return `data:${asset.mime};base64,${fs.readFileSync(file).toString("base64")}`;
}

const officialImage = imageDataUrl(brief.official_image);

function rowsFor(numbers) {
  return numbers.map((number) => {
    const item = brief.items[number - 1];
    return `<div class="brief-row">
      <p class="brief-num">${String(number).padStart(2, "0")}</p>
      <div class="brief-copy">
        <h3>${escape(item.title)}</h3>
        <p>${escape(item.source)} · ${escape(formatItemTime(item.published_at))}</p>
        <p class="brief-impact">${escape(item.impact)}</p>
      </div>
    </div>`;
  }).join("\n");
}

function formatItemTime(value) {
  try {
    return new Intl.DateTimeFormat("zh-CN", {
      timeZone: "Asia/Shanghai", month: "numeric", day: "numeric",
      hour: "2-digit", minute: "2-digit", hour12: false,
    }).format(new Date(value));
  } catch {
    return "时间待核对";
  }
}

function commonHeader(cardIndex) {
  return `<div class="chrome-min">
    <span>SHARK BRIEF / 鲨鲨简报</span>
    <span>${escape(dateLabel)} · ${String(cardIndex).padStart(2, "0")}/${String(brief.cards.length).padStart(2, "0")}</span>
  </div>`;
}

function s01(card, cardIndex) {
  const items = card.item_numbers.map((number) => brief.items[number - 1]);
  const first = items[0];
  const title = first?.title || "本窗口无重要更新";
  const second = items[1];
  return `<section class="poster xhs brief-card" id="brief-card-${cardIndex}">
    <div class="content stack gap-9">
      ${commonHeader(cardIndex)}
      <div class="stack gap-7">
        <p class="t-cat">${escape(slotLabel)} · TOP SIGNAL</p>
        <h1 class="h-statement brief-cover-title">${escape(title)}</h1>
      </div>
      <div class="grow"></div>
      <hr class="hr-accent">
      <p class="lead">${escape(first?.impact || "本时间窗不为凑数制造新闻。")}</p>
      ${second ? `<div class="brief-secondary"><span>02</span><strong>${escape(second.title)}</strong></div>` : ""}
      <div class="row gap-6"><p class="t-meta">REAL SOURCES</p><p class="t-meta">/ 14-DAY DEDUPE</p></div>
    </div>
  </section>`;
}

function s04(card, cardIndex) {
  const first = brief.items[card.item_numbers[0] - 1];
  return `<section class="poster xhs brief-card" id="brief-card-${cardIndex}">
    <div class="content stack gap-7">
      ${commonHeader(cardIndex)}
      <div class="stack gap-5">
        <p class="t-cat">${escape(slotLabel)} · OFFICIAL EVIDENCE</p>
        <h1 class="h-md brief-interface-title">${escape(first.title)}</h1>
      </div>
      <div class="device-browser brief-browser">
        <div class="frame-shot bg-paper inset-sub"><img src="${officialImage}" alt="官方来源图片"></div>
      </div>
      <p class="swiss-img-caption">OFFICIAL OG · ${escape(brief.official_image?.domain || first.source)}</p>
      <div class="brief-compact-rows">${rowsFor(card.item_numbers)}</div>
      <div class="grow"></div>
      <p class="t-meta">图片不叠文字 · 原链接见消息正文</p>
    </div>
  </section>`;
}

function ledger(card, cardIndex) {
  return `<section class="poster xhs brief-card" id="brief-card-${cardIndex}">
    <div class="content stack gap-7">
      ${commonHeader(cardIndex)}
      <p class="t-cat">${escape(slotLabel)} · RANKED SIGNALS</p>
      <h2 class="h-xl brief-ledger-title">值得知道的<br>AI 变化</h2>
      <div class="brief-rows">${rowsFor(card.item_numbers)}</div>
      <div class="grow"></div>
      <div class="brief-footer"><span>排序按判断价值</span><span>不制造数据</span></div>
    </div>
  </section>`;
}

const posters = brief.cards.map((card, index) => {
  const cardIndex = index + 1;
  if (cardIndex === 1 && officialImage) return s04(card, cardIndex);
  if (card.item_numbers.length <= 2) return s01(card, cardIndex);
  return ledger(card, cardIndex);
}).join("\n");

const taskCss = `
    /* ACT SHARK BRIEF — task-scoped deterministic additions. */
    .brief-card { background: var(--paper); }
    .brief-card { --sans: "Inter", "WenQuanYi Zen Hei", sans-serif; --sans-zh: "WenQuanYi Zen Hei", "Noto Sans SC", sans-serif; }
    .brief-card .content { min-height: 0; }
    .brief-cover-title { font-size: 78px !important; line-height: 1.12; overflow-wrap: anywhere; }
    .brief-ledger-title { font-size: 74px !important; line-height: 1.08; }
    .brief-interface-title { font-size: 55px; line-height: 1.16; overflow-wrap: anywhere; }
    .brief-browser { height: 420px; }
    .brief-browser .frame-shot { height: calc(100% - 32px); }
    .brief-browser img { width: 100%; height: 100%; object-fit: contain; object-position: center; }
    .brief-rows, .brief-compact-rows { display: flex; flex-direction: column; border-top: 1px solid var(--grey-2); }
    .brief-compact-rows { margin-top: 6px; }
    .brief-row { display: grid; grid-template-columns: 104px 1fr; gap: 28px; padding: 28px 0; border-bottom: 1px solid var(--grey-2); min-height: 170px; }
    .brief-compact-rows .brief-row { min-height: 0; padding: 14px 0; grid-template-columns: 76px 1fr; gap: 20px; }
    .brief-num { margin: 0; font-family: var(--sans); font-weight: 200; font-size: 66px; line-height: .95; color: var(--accent); }
    .brief-compact-rows .brief-num { font-size: 48px; }
    .brief-copy { min-width: 0; }
    .brief-copy h3 { margin: 0; font-family: var(--sans-zh); font-size: 30px; font-weight: 500; line-height: 1.25; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
    .brief-copy p { margin: 9px 0 0; font-family: var(--mono); color: var(--grey-3); font-size: 18px; line-height: 1.25; }
    .brief-copy .brief-impact { font-family: var(--sans-zh); font-size: 21px; line-height: 1.35; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
    .brief-compact-rows .brief-impact { display: none; }
    .brief-secondary { display: grid; grid-template-columns: 72px 1fr; gap: 24px; padding: 24px 0; border-top: 1px solid var(--grey-2); border-bottom: 1px solid var(--grey-2); font-family: var(--sans-zh); font-size: 26px; line-height: 1.3; }
    .brief-secondary span { color: var(--accent); font-family: var(--mono); font-weight: 500; }
    .brief-secondary strong { font-weight: 500; }
    .brief-footer { display: flex; justify-content: space-between; padding-top: 20px; border-top: 1px solid var(--grey-2); color: var(--grey-3); font: 500 18px/1 var(--mono); letter-spacing: .1em; text-transform: uppercase; }
  `;

// The vendored upstream seed is the source. Remove its network font/icon tags,
// inject one task CSS block, and replace only the POSTERS_HERE content region.
template = template
  .replace(/\s*<link[^>]+fonts\.(?:googleapis|gstatic)\.com[^>]*>/g, "")
  .replace(/\s*<script[^>]+unpkg\.com\/lucide[^>]*><\/script>/g, "")
  .replace("</style>", `${taskCss}</style>`)
  .replace("[必填] 替换为社交卡组标题 · Social Card Set", `SHARK BRIEF · ${slotLabel}`);

const marker = "<!-- POSTERS_HERE -->";
const markerIndex = template.lastIndexOf(marker);
const mainEnd = template.indexOf("</main>", markerIndex);
if (markerIndex < 0 || mainEnd < 0) throw new Error("vendored seed marker missing");
const html = template.slice(0, markerIndex) + marker + "\n" + posters + "\n  " + template.slice(mainEnd);
const htmlPath = path.join(outputDir, "index.html");
fs.writeFileSync(htmlPath, html, { mode: 0o600 });

const launchOptions = { headless: true };
if (process.env.ACT_BRIEF_CHROMIUM_EXECUTABLE) {
  launchOptions.executablePath = process.env.ACT_BRIEF_CHROMIUM_EXECUTABLE;
}
const browser = await chromium.launch(launchOptions);
try {
  const page = await browser.newPage({ viewport: { width: 1240, height: 1600 }, deviceScaleFactor: 1 });
  await page.route(/^https?:\/\//, (route) => route.abort("blockedbyclient"));
  await page.goto(pathToFileURL(htmlPath).href, { waitUntil: "load" });
  const cards = page.locator("section.poster.xhs");
  const count = await cards.count();
  if (count !== brief.cards.length) throw new Error(`expected ${brief.cards.length} cards, found ${count}`);
  const files = [];
  for (let index = 0; index < count; index += 1) {
    const file = path.join(outputDir, `brief-card-${String(index + 1).padStart(2, "0")}.png`);
    await cards.nth(index).screenshot({ path: file });
    fs.chmodSync(file, 0o600);
    files.push(file);
  }
  process.stdout.write(JSON.stringify({ files, html: htmlPath }));
} finally {
  await browser.close();
}
