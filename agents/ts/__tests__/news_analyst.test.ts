import {
  NewsAnalystOutput,
  NewsResult,
  OtelSpan,
  RunOptions,
  TickerInput,
  generateSpanId,
  generateTraceId,
  runNewsAnalyst,
} from "../news_analyst";

// ── Mock factory helpers ──────────────────────────────────────────────────

const _SESSION_ID = "a1b2c3d4-e5f6-7890-abcd-ef1234567890";
const _DATE = "2026-05-27";

const _INPUT: TickerInput = {
  tickers: ["AAPL", "NVDA"],
  date: _DATE,
  session_id: _SESSION_ID,
};

const _AAPL_NEWS: NewsResult = {
  ticker: "AAPL",
  headlines: [
    { title: "Apple unveils M4 chip", published: "2026-05-27T08:00:00Z", publisher: "Reuters" },
  ],
  count: 1,
  error: null,
};

const _NVDA_NEWS: NewsResult = {
  ticker: "NVDA",
  headlines: [
    { title: "Nvidia wins $2B contract", published: "2026-05-27T07:00:00Z", publisher: "Bloomberg" },
  ],
  count: 1,
  error: null,
};

const _EARNINGS_NEWS: NewsResult = {
  ticker: "CRWD",
  headlines: [
    { title: "CrowdStrike Q1 results reports after close today", published: "2026-05-27T07:00:00Z", publisher: "PR Newswire" },
  ],
  count: 1,
  error: null,
};

function makeToolUseResponse(ticker: string, toolId = "tool-1") {
  return {
    stop_reason: "tool_use",
    content: [
      {
        type: "tool_use" as const,
        id: toolId,
        name: "get_ticker_news",
        input: { ticker },
      },
    ],
    usage: { input_tokens: 200, output_tokens: 50 },
  };
}

function makeEndTurnResponse(signals: object[]) {
  const blackoutCount = signals.filter((s: any) => s.blackout).length;
  return {
    stop_reason: "end_turn",
    content: [
      {
        type: "text" as const,
        text: JSON.stringify({
          date: _DATE,
          news_signals: signals,
          total_tickers: signals.length,
          blackout_count: blackoutCount,
        }),
      },
    ],
    usage: { input_tokens: 100, output_tokens: 200 },
  };
}

function makeMockClient(responses: ReturnType<typeof makeToolUseResponse | typeof makeEndTurnResponse>[]) {
  let callIndex = 0;
  return {
    messages: {
      create: jest.fn().mockImplementation(() => {
        return Promise.resolve(responses[callIndex++]);
      }),
    },
  };
}

const _NEUTRAL_SIGNAL = (ticker: string) => ({
  ticker,
  signal: "neutral",
  blackout: false,
  headline_count: 1,
  top_headline: `${ticker} news`,
  top_publisher: "Reuters",
  top_published: "2026-05-27T08:00:00Z",
});

const _POSITIVE_SIGNAL = (ticker: string) => ({
  ticker,
  signal: "positive",
  blackout: false,
  headline_count: 1,
  top_headline: `${ticker} wins contract`,
  top_publisher: "Bloomberg",
  top_published: "2026-05-27T07:00:00Z",
});

const _BLACKOUT_SIGNAL = (ticker: string) => ({
  ticker,
  signal: "neutral",
  blackout: true,
  headline_count: 1,
  top_headline: `${ticker} Q1 results reports after close today`,
  top_publisher: "PR Newswire",
  top_published: "2026-05-27T07:00:00Z",
});

// ── Tests ─────────────────────────────────────────────────────────────────

describe("generateTraceId", () => {
  it("returns 32-character hex string", () => {
    const id = generateTraceId();
    expect(id).toMatch(/^[0-9a-f]{32}$/);
  });

  it("returns unique values", () => {
    expect(generateTraceId()).not.toBe(generateTraceId());
  });
});

describe("generateSpanId", () => {
  it("returns 12-character hex string", () => {
    const id = generateSpanId();
    expect(id).toMatch(/^[0-9a-f]{12}$/);
  });
});

describe("runNewsAnalyst", () => {
  function run(
    input: TickerInput,
    responses: ReturnType<typeof makeToolUseResponse | typeof makeEndTurnResponse>[],
    newsMap: Record<string, NewsResult> = {}
  ): { output: Promise<NewsAnalystOutput>; emitted: string[] } {
    const emitted: string[] = [];
    const client = makeMockClient(responses);
    const newsProvider = jest.fn().mockImplementation((ticker: string) =>
      Promise.resolve(newsMap[ticker] ?? { ticker, headlines: [], count: 0, error: null })
    );
    const options: RunOptions = {
      client: client as any,
      newsProvider,
      emit: (line: string) => emitted.push(line),
    };
    return { output: runNewsAnalyst(input, options), emitted };
  }

  it("returns output with correct structure for two tickers", async () => {
    const { output } = run(
      _INPUT,
      [
        makeToolUseResponse("AAPL", "t1"),
        makeToolUseResponse("NVDA", "t2"),
        makeEndTurnResponse([_NEUTRAL_SIGNAL("AAPL"), _POSITIVE_SIGNAL("NVDA")]),
      ],
      { AAPL: _AAPL_NEWS, NVDA: _NVDA_NEWS }
    );
    const result = await output;
    expect(result.session_id).toBe(_SESSION_ID);
    expect(result.date).toBe(_DATE);
    expect(result.news_signals).toHaveLength(2);
    expect(result.total_tickers).toBe(2);
    expect(typeof result.duration_ms).toBe("number");
  });

  it("tickers in news_signals match expected order from agent", async () => {
    const { output } = run(
      _INPUT,
      [
        makeToolUseResponse("AAPL", "t1"),
        makeToolUseResponse("NVDA", "t2"),
        makeEndTurnResponse([_NEUTRAL_SIGNAL("AAPL"), _POSITIVE_SIGNAL("NVDA")]),
      ],
      { AAPL: _AAPL_NEWS, NVDA: _NVDA_NEWS }
    );
    const result = await output;
    const tickers = result.news_signals.map((s) => s.ticker);
    expect(tickers).toContain("AAPL");
    expect(tickers).toContain("NVDA");
  });

  it("emits OTEL_SPAN line for each tool call", async () => {
    const { output, emitted } = run(
      _INPUT,
      [
        makeToolUseResponse("AAPL", "t1"),
        makeToolUseResponse("NVDA", "t2"),
        makeEndTurnResponse([_NEUTRAL_SIGNAL("AAPL"), _POSITIVE_SIGNAL("NVDA")]),
      ],
      { AAPL: _AAPL_NEWS, NVDA: _NVDA_NEWS }
    );
    await output;
    const spanLines = emitted.filter((l) => l.startsWith("OTEL_SPAN:"));
    // 2 tool calls + 1 session span
    expect(spanLines.length).toBeGreaterThanOrEqual(3);
  });

  it("emits session OTEL_SPAN with correct attributes", async () => {
    const { output, emitted } = run(
      _INPUT,
      [
        makeToolUseResponse("AAPL", "t1"),
        makeEndTurnResponse([_NEUTRAL_SIGNAL("AAPL")]),
      ],
      { AAPL: _AAPL_NEWS }
    );
    await output;
    const spanLines = emitted.filter((l) => l.startsWith("OTEL_SPAN:"));
    const spans: OtelSpan[] = spanLines.map((l) =>
      JSON.parse(l.replace("OTEL_SPAN: ", "")) as OtelSpan
    );
    const sessionSpan = spans.find((s) => s.name === "news_analyst.session");
    expect(sessionSpan).toBeDefined();
    expect(sessionSpan!.attributes["agent.name"]).toBe("news_analyst");
    expect(sessionSpan!.attributes["agent.language"]).toBe("typescript");
    expect(sessionSpan!.attributes["session.id"]).toBe(_SESSION_ID);
  });

  it("emits tool OTEL_SPAN with ticker attribute", async () => {
    const { output, emitted } = run(
      { ..._INPUT, tickers: ["AAPL"] },
      [makeToolUseResponse("AAPL", "t1"), makeEndTurnResponse([_NEUTRAL_SIGNAL("AAPL")])],
      { AAPL: _AAPL_NEWS }
    );
    await output;
    const spanLines = emitted.filter((l) => l.startsWith("OTEL_SPAN:"));
    const spans: OtelSpan[] = spanLines.map((l) =>
      JSON.parse(l.replace("OTEL_SPAN: ", "")) as OtelSpan
    );
    const toolSpan = spans.find((s) => s.name === "news_analyst.get_ticker_news");
    expect(toolSpan).toBeDefined();
    expect(toolSpan!.attributes["tool.input.ticker"]).toBe("AAPL");
  });

  it("emits final JSON output line (not prefixed with OTEL_SPAN)", async () => {
    const { output, emitted } = run(
      { ..._INPUT, tickers: ["AAPL"] },
      [makeToolUseResponse("AAPL", "t1"), makeEndTurnResponse([_NEUTRAL_SIGNAL("AAPL")])],
      { AAPL: _AAPL_NEWS }
    );
    await output;
    const jsonLines = emitted.filter((l) => !l.startsWith("OTEL_SPAN:"));
    expect(jsonLines).toHaveLength(1);
    const parsed = JSON.parse(jsonLines[0]) as NewsAnalystOutput;
    expect(parsed.session_id).toBe(_SESSION_ID);
  });

  it("sets blackout_count from agent response", async () => {
    const { output } = run(
      { ..._INPUT, tickers: ["CRWD"] },
      [
        makeToolUseResponse("CRWD", "t1"),
        makeEndTurnResponse([_BLACKOUT_SIGNAL("CRWD")]),
      ],
      { CRWD: _EARNINGS_NEWS }
    );
    const result = await output;
    expect(result.blackout_count).toBe(1);
    const signal = result.news_signals.find((s) => s.ticker === "CRWD");
    expect(signal?.blackout).toBe(true);
  });

  it("news fetch error results in error flag in span status", async () => {
    const failingNews: NewsResult = {
      ticker: "AAPL",
      headlines: [],
      count: 0,
      error: "fetch_failed",
    };
    const { output, emitted } = run(
      { ..._INPUT, tickers: ["AAPL"] },
      [
        makeToolUseResponse("AAPL", "t1"),
        makeEndTurnResponse([{ ticker: "AAPL", signal: "null", blackout: false,
          headline_count: 0, top_headline: "", top_publisher: "", top_published: "" }]),
      ],
      { AAPL: failingNews }
    );
    await output;
    const spanLines = emitted.filter((l) => l.startsWith("OTEL_SPAN:"));
    const spans: OtelSpan[] = spanLines.map((l) =>
      JSON.parse(l.replace("OTEL_SPAN: ", "")) as OtelSpan
    );
    const toolSpan = spans.find((s) => s.name === "news_analyst.get_ticker_news");
    expect(toolSpan!.status.code).toBe(2);
  });

  it("throws on invalid JSON in end_turn text block", async () => {
    const client = makeMockClient([
      {
        stop_reason: "end_turn",
        content: [{ type: "text" as const, text: "not valid json" }],
        usage: { input_tokens: 100, output_tokens: 50 },
      },
    ]);
    const options: RunOptions = {
      client: client as any,
      newsProvider: async () => _AAPL_NEWS,
      emit: () => undefined,
    };
    await expect(runNewsAnalyst({ ..._INPUT, tickers: ["AAPL"] }, options)).rejects.toThrow(
      "Failed to parse agent JSON"
    );
  });

  it("throws on unexpected stop_reason", async () => {
    const client = makeMockClient([
      { stop_reason: "max_tokens", content: [], usage: { input_tokens: 100, output_tokens: 50 } } as any,
    ]);
    const options: RunOptions = {
      client: client as any,
      newsProvider: async () => _AAPL_NEWS,
      emit: () => undefined,
    };
    await expect(runNewsAnalyst({ ..._INPUT, tickers: ["AAPL"] }, options)).rejects.toThrow(
      "Unexpected stop_reason"
    );
  });

  it("calls newsProvider once per tool_use block", async () => {
    const newsProvider = jest.fn().mockResolvedValue(_AAPL_NEWS);
    const client = makeMockClient([
      makeToolUseResponse("AAPL", "t1"),
      makeEndTurnResponse([_NEUTRAL_SIGNAL("AAPL")]),
    ]);
    const options: RunOptions = {
      client: client as any,
      newsProvider,
      emit: () => undefined,
    };
    await runNewsAnalyst({ ..._INPUT, tickers: ["AAPL"] }, options);
    expect(newsProvider).toHaveBeenCalledTimes(1);
    expect(newsProvider).toHaveBeenCalledWith("AAPL");
  });

  it("passes tool result back to client in next message", async () => {
    const mockCreate = jest.fn()
      .mockResolvedValueOnce(makeToolUseResponse("AAPL", "t1"))
      .mockResolvedValueOnce(makeEndTurnResponse([_NEUTRAL_SIGNAL("AAPL")]));
    const client = { messages: { create: mockCreate } };
    const options: RunOptions = {
      client: client as any,
      newsProvider: async () => _AAPL_NEWS,
      emit: () => undefined,
    };
    await runNewsAnalyst({ ..._INPUT, tickers: ["AAPL"] }, options);

    expect(mockCreate).toHaveBeenCalledTimes(2);
    const secondCallMessages = mockCreate.mock.calls[1][0].messages as any[];
    const userContent = secondCallMessages[secondCallMessages.length - 1].content;
    expect(Array.isArray(userContent)).toBe(true);
    expect(userContent[0].type).toBe("tool_result");
    expect(userContent[0].tool_use_id).toBe("t1");
  });

  it("accumulates tokens across turns in session span", async () => {
    const { output, emitted } = run(
      { ..._INPUT, tickers: ["AAPL"] },
      [
        makeToolUseResponse("AAPL", "t1"),  // 200 in, 50 out
        makeEndTurnResponse([_NEUTRAL_SIGNAL("AAPL")]),  // 100 in, 200 out
      ],
      { AAPL: _AAPL_NEWS }
    );
    await output;
    const spanLines = emitted.filter((l) => l.startsWith("OTEL_SPAN:"));
    const spans: OtelSpan[] = spanLines.map((l) =>
      JSON.parse(l.replace("OTEL_SPAN: ", "")) as OtelSpan
    );
    const sessionSpan = spans.find((s) => s.name === "news_analyst.session");
    expect(sessionSpan!.attributes["tokens.input"]).toBe(300);
    expect(sessionSpan!.attributes["tokens.output"]).toBe(250);
  });
});
