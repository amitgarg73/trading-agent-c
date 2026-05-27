"use strict";
var __importDefault = (this && this.__importDefault) || function (mod) {
    return (mod && mod.__esModule) ? mod : { "default": mod };
};
Object.defineProperty(exports, "__esModule", { value: true });
exports.generateTraceId = generateTraceId;
exports.generateSpanId = generateSpanId;
exports.fetchTickerNews = fetchTickerNews;
exports.runNewsAnalyst = runNewsAnalyst;
const sdk_1 = __importDefault(require("@anthropic-ai/sdk"));
const child_process_1 = require("child_process");
const crypto_1 = require("crypto");
// ── Constants ──────────────────────────────────────────────────────────────
const MODEL = "claude-haiku-4-5-20251001";
const SYSTEM_PROMPT = `You are a financial news sentiment classifier.

You receive a list of stock tickers. For each ticker, call get_ticker_news once.
Do not skip any ticker. Do not call get_ticker_news more than once per ticker.

After all news calls, classify each ticker's signal:

  positive:  clear bullish catalyst — earnings beat, analyst upgrade, contract win,
             FDA approval, partnership announcement with revenue impact
  negative:  clear bearish catalyst — earnings miss, analyst downgrade, lawsuit,
             product recall, regulatory action, executive departure (CEO/CFO)
  neutral:   no relevant news today, or mixed signals with no dominant direction
  null:      news API returned empty — treat as neutral for scoring but flag it

Also set blackout: true if any headline mentions earnings today or tomorrow,
or if the news contains "Q[1-4] results", "reports after close", "reports before open",
"earnings call scheduled". Blackout overrides all other classification.

Return JSON only. No commentary before or after. Use this exact structure:
{
  "date": "YYYY-MM-DD",
  "news_signals": [
    {
      "ticker": "AAPL",
      "signal": "neutral",
      "blackout": false,
      "headline_count": 2,
      "top_headline": "Apple unveils new MacBook",
      "top_publisher": "Reuters",
      "top_published": "2026-05-27T10:30:00Z"
    }
  ],
  "total_tickers": 1,
  "blackout_count": 0
}`;
const GET_TICKER_NEWS_TOOL = {
    name: "get_ticker_news",
    description: "Fetch recent news headlines for a stock ticker",
    input_schema: {
        type: "object",
        properties: {
            ticker: { type: "string", description: "Stock ticker symbol, e.g. AAPL" },
        },
        required: ["ticker"],
    },
};
// ── Helpers ────────────────────────────────────────────────────────────────
function generateTraceId() {
    return (0, crypto_1.randomBytes)(16).toString("hex");
}
function generateSpanId() {
    return (0, crypto_1.randomBytes)(6).toString("hex");
}
async function fetchTickerNews(ticker) {
    try {
        const raw = (0, child_process_1.execSync)(`python3 agents/tools/news_tools_helper.py ${ticker}`, { encoding: "utf-8", timeout: 5000 });
        return JSON.parse(raw.trim());
    }
    catch {
        return { ticker, headlines: [], count: 0, error: "fetch_failed" };
    }
}
// ── Core agent ─────────────────────────────────────────────────────────────
async function runNewsAnalyst(input, options = {}) {
    const startTime = Date.now();
    const client = options.client ?? new sdk_1.default();
    const newsProvider = options.newsProvider ?? fetchTickerNews;
    const emit = options.emit ??
        ((line) => {
            process.stdout.write(line + "\n");
        });
    const traceId = generateTraceId();
    const sessionSpanId = generateSpanId();
    const messages = [
        {
            role: "user",
            content: `Classify news sentiment for these tickers: ${input.tickers.join(", ")}\n` +
                `Date: ${input.date}`,
        },
    ];
    let totalInputTokens = 0;
    let totalOutputTokens = 0;
    while (true) {
        const response = await client.messages.create({
            model: MODEL,
            max_tokens: 1024,
            system: SYSTEM_PROMPT,
            messages,
            tools: [GET_TICKER_NEWS_TOOL],
        });
        totalInputTokens += response.usage.input_tokens;
        totalOutputTokens += response.usage.output_tokens;
        if (response.stop_reason === "tool_use") {
            const toolResults = [];
            for (const block of response.content) {
                if (block.type === "tool_use") {
                    const ticker = block.input.ticker;
                    const callStart = Date.now();
                    const result = await newsProvider(ticker);
                    const callEnd = Date.now();
                    const span = {
                        traceId,
                        spanId: generateSpanId(),
                        parentSpanId: sessionSpanId,
                        name: `news_analyst.${block.name}`,
                        kind: 3,
                        startTimeUnixNano: callStart * 1000000,
                        endTimeUnixNano: callEnd * 1000000,
                        attributes: {
                            "agent.name": "news_analyst",
                            "agent.language": "typescript",
                            "tool.name": block.name,
                            "tool.input.ticker": ticker,
                            "tool.output.count": result.count,
                            "tool.output.error": result.error ?? "",
                            "session.id": input.session_id,
                            model: MODEL,
                        },
                        status: { code: result.error ? 2 : 1 },
                    };
                    emit(`OTEL_SPAN: ${JSON.stringify(span)}`);
                    toolResults.push({
                        type: "tool_result",
                        tool_use_id: block.id,
                        content: JSON.stringify(result),
                    });
                }
            }
            messages.push({ role: "assistant", content: response.content });
            messages.push({ role: "user", content: toolResults });
        }
        else if (response.stop_reason === "end_turn") {
            const textBlock = response.content.find((b) => b.type === "text");
            if (!textBlock || textBlock.type !== "text") {
                throw new Error("No text block in end_turn response");
            }
            let parsed;
            try {
                parsed = JSON.parse(textBlock.text);
            }
            catch {
                throw new Error(`Failed to parse agent JSON: ${textBlock.text.slice(0, 200)}`);
            }
            const output = {
                date: parsed.date ?? input.date,
                session_id: input.session_id,
                news_signals: parsed.news_signals ?? [],
                total_tickers: parsed.total_tickers ?? input.tickers.length,
                blackout_count: parsed.blackout_count ?? 0,
                duration_ms: Date.now() - startTime,
            };
            const sessionSpan = {
                traceId,
                spanId: sessionSpanId,
                parentSpanId: input.session_id.replace(/-/g, "").slice(0, 12),
                name: "news_analyst.session",
                kind: 2,
                startTimeUnixNano: startTime * 1000000,
                endTimeUnixNano: Date.now() * 1000000,
                attributes: {
                    "agent.name": "news_analyst",
                    "agent.language": "typescript",
                    "tickers.analyzed": input.tickers.length,
                    "blackout.count": output.blackout_count,
                    "tokens.input": totalInputTokens,
                    "tokens.output": totalOutputTokens,
                    duration_ms: output.duration_ms,
                    "session.id": input.session_id,
                    model: MODEL,
                },
                status: { code: 1 },
            };
            emit(`OTEL_SPAN: ${JSON.stringify(sessionSpan)}`);
            emit(JSON.stringify(output));
            return output;
        }
        else {
            throw new Error(`Unexpected stop_reason: ${response.stop_reason}`);
        }
    }
}
// ── Entry point ────────────────────────────────────────────────────────────
if (require.main === module) {
    const rawInput = process.argv[2];
    if (!rawInput) {
        process.stderr.write("Usage: node news_analyst.js '<json_input>'\n");
        process.exit(1);
    }
    let input;
    try {
        input = JSON.parse(rawInput);
    }
    catch {
        process.stderr.write("Invalid JSON input\n");
        process.exit(1);
    }
    runNewsAnalyst(input).catch((err) => {
        process.stderr.write(`News Analyst error: ${err.message}\n`);
        process.exit(1);
    });
}
//# sourceMappingURL=news_analyst.js.map