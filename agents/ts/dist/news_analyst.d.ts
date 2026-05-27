import Anthropic from "@anthropic-ai/sdk";
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
    status: {
        code: number;
    };
}
export interface RunOptions {
    client?: Anthropic;
    newsProvider?: (ticker: string) => Promise<NewsResult>;
    emit?: (line: string) => void;
}
export declare function generateTraceId(): string;
export declare function generateSpanId(): string;
export declare function fetchTickerNews(ticker: string): Promise<NewsResult>;
export declare function runNewsAnalyst(input: TickerInput, options?: RunOptions): Promise<NewsAnalystOutput>;
//# sourceMappingURL=news_analyst.d.ts.map