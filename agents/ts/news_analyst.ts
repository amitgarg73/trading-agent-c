import Anthropic from "@anthropic-ai/sdk";
import { randomBytes } from "crypto";

// ── Types ──────────────────────────────────────────────────────────────────

export interface TickerInput {
  tickers: string[];
  date: string;
  session_id: string;
}

export interface NewsHeadline {
  title: string;
  published: string;
  publisher: string;
}

export interface NewsResult {
  ticker: string;
  headlines: NewsHeadline[];
  count: number;
  error: string | null;
}

export interface NewsSignal {
  ticker: string;
  signal: "positive" | "negative" | "neutral" | "null";
  blackout: boolean;
  headline_count: number;
  top_headline: string;
  top_publisher: string;
  top_published: string;
}

export interface NewsAnalystOutput {
  date: string;
  session_id: string;
  news_signals: NewsSignal[];
  total_tickers: number;
  blackout_count: number;
  duration_ms: number;
}

export interface OtelSpan {
  traceId: string;
  spanId: string;
  parentSpanId: string;
  name: string;
  kind: number;
  startTimeUnixNano: number;
  endTimeUnixNano: number;
  attributes: Record<string, string | number | boolean>;
  status: { code: number };
}

export interface RunOptions {
  client?: Anthropic;
  newsProvider?: (ticker: string) => Promise<NewsResult>;
  emit?: (line: string) => void;
}

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

const GET_TICKER_NEWS_TOOL: Anthropic.Tool = {
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

export function generateTraceId(): string {
  return randomBytes(16).toString("hex");
}

export function generateSpanId(): string {
  return randomBytes(6).toString("hex");
}

export async function fetchTickerNews(ticker: string): Promise<NewsResult> {
  const apiKey    = process.env.APCA_API_KEY_ID ?? "";
  const apiSecret = process.env.APCA_API_SECRET_KEY ?? "";

  if (!apiKey || !apiSecret) {
    return { ticker, headlines: [], count: 0, error: "missing_alpaca_credentials" };
  }

  try {
    const start = new Date(Date.now() - 7 * 24 * 60 * 60 * 1000).toISOString();
    const url   = `https://data.alpaca.markets/v1beta1/news?symbols=${encodeURIComponent(ticker)}&limit=5&start=${encodeURIComponent(start)}`;

    const res = await fetch(url, {
      headers: {
        "APCA-API-KEY-ID":     apiKey,
        "APCA-API-SECRET-KEY": apiSecret,
      },
    });

    if (!res.ok) {
      return { ticker, headlines: [], count: 0, error: `alpaca_http_${res.status}` };
    }

    const data = (await res.json()) as { news?: Array<{ headline: string; created_at: string; source: string }> };
    const items = data.news ?? [];
    const headlines: NewsHeadline[] = items.map((item) => ({
      title:     item.headline,
      published: item.created_at,
      publisher: item.source,
    }));

    return { ticker, headlines, count: headlines.length, error: null };
  } catch (err) {
    return { ticker, headlines: [], count: 0, error: String(err) };
  }
}

// ── Core agent ─────────────────────────────────────────────────────────────

export async function runNewsAnalyst(
  input: TickerInput,
  options: RunOptions = {}
): Promise<NewsAnalystOutput> {
  const startTime = Date.now();
  const client = options.client ?? new Anthropic();
  const newsProvider = options.newsProvider ?? fetchTickerNews;
  const emit =
    options.emit ??
    ((line: string) => {
      process.stdout.write(line + "\n");
    });

  const traceId = generateTraceId();
  const sessionSpanId = generateSpanId();

  const messages: Anthropic.MessageParam[] = [
    {
      role: "user",
      content:
        `Classify news sentiment for these tickers: ${input.tickers.join(", ")}\n` +
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
      const toolResults: Anthropic.ToolResultBlockParam[] = [];

      for (const block of response.content) {
        if (block.type === "tool_use") {
          const ticker = (block.input as { ticker: string }).ticker;
          const callStart = Date.now();
          const result = await newsProvider(ticker);
          const callEnd = Date.now();

          const span: OtelSpan = {
            traceId,
            spanId: generateSpanId(),
            parentSpanId: sessionSpanId,
            name: `news.${block.name}`,
            kind: 3,
            startTimeUnixNano: callStart * 1_000_000,
            endTimeUnixNano: callEnd * 1_000_000,
            attributes: {
              "agent.name": "news",
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
    } else if (response.stop_reason === "end_turn") {
      const textBlock = response.content.find((b) => b.type === "text");
      if (!textBlock || textBlock.type !== "text") {
        throw new Error("No text block in end_turn response");
      }

      let parsed: Partial<NewsAnalystOutput>;
      try {
        parsed = JSON.parse(textBlock.text) as Partial<NewsAnalystOutput>;
      } catch {
        throw new Error(
          `Failed to parse agent JSON: ${textBlock.text.slice(0, 200)}`
        );
      }

      const output: NewsAnalystOutput = {
        date: parsed.date ?? input.date,
        session_id: input.session_id,
        news_signals: parsed.news_signals ?? [],
        total_tickers: parsed.total_tickers ?? input.tickers.length,
        blackout_count: parsed.blackout_count ?? 0,
        duration_ms: Date.now() - startTime,
      };

      // Emit a reasoning span so the embeddings job can embed this agent's output.
      // The normalizer maps agent.reasoning → payload.agent_reasoning for step_type=agent_message.
      const reasoningSpan: OtelSpan = {
        traceId,
        spanId: generateSpanId(),
        parentSpanId: sessionSpanId,
        name: "news.reasoning",
        kind: 2,
        startTimeUnixNano: startTime * 1_000_000,
        endTimeUnixNano: Date.now() * 1_000_000,
        attributes: {
          "agent.name": "news",
          "agent.language": "typescript",
          "agent.reasoning": textBlock.text,
          "tokens.input": totalInputTokens,
          "tokens.output": totalOutputTokens,
          "session.id": input.session_id,
          model: MODEL,
        },
        status: { code: 1 },
      };
      emit(`OTEL_SPAN: ${JSON.stringify(reasoningSpan)}`);

      const sessionSpan: OtelSpan = {
        traceId,
        spanId: sessionSpanId,
        parentSpanId: input.session_id.replace(/-/g, "").slice(0, 12),
        name: "news.session",
        kind: 2,
        startTimeUnixNano: startTime * 1_000_000,
        endTimeUnixNano: Date.now() * 1_000_000,
        attributes: {
          "agent.name": "news",
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
    } else {
      throw new Error(`Unexpected stop_reason: ${response.stop_reason}`);
    }
  }
}

// ── Entry point ────────────────────────────────────────────────────────────

if (require.main === module) {
  const rawInput = process.argv[2];
  if (!rawInput) {
    process.stderr.write(
      "Usage: node news_analyst.js '<json_input>'\n"
    );
    process.exit(1);
  }

  let input: TickerInput;
  try {
    input = JSON.parse(rawInput) as TickerInput;
  } catch {
    process.stderr.write("Invalid JSON input\n");
    process.exit(1);
  }

  runNewsAnalyst(input).catch((err: Error) => {
    process.stderr.write(`News Analyst error: ${err.message}\n`);
    process.exit(1);
  });
}
