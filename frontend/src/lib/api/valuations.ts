/**
 * Valuations API - used-market value estimates.
 *
 * Homebox is the source of truth for the value (a custom field). Estimates
 * are proposals until approved; only `apply` writes to Homebox.
 */

import { request } from './client';

export type ValuationJobMode = 'missing' | 'refresh';
export type ValuationJobStatus =
	'snapshot' | 'running' | 'completed' | 'cancelled' | 'interrupted' | 'failed';

export interface ValuationSource {
	title: string;
	url: string;
	price: string | null;
}

export interface Valuation {
	value: number;
	currency: string;
	low: number | null;
	high: number | null;
	confidence: 'low' | 'medium' | 'high';
	reasoning: string;
	sources: ValuationSource[];
	valuedAt: string;
}

/** The approved value, read from the item's custom field in Homebox */
export interface HomeboxValue {
	value: number;
	currency: string | null;
	text: string;
}

export interface ValuedItem {
	id: string;
	name: string;
	thumbnailId: string | null;
	homeboxValue: HomeboxValue | null;
	/** Latest estimate; awaiting approval when `pending` is true */
	estimate: Valuation | null;
	pending: boolean;
	/** Percent change from the Homebox value to the pending estimate */
	changePct: number | null;
	lastRunId: string | null;
	lastError: string | null;
}

export interface ApplyResult {
	applied: string[];
	errors: Record<string, string>;
}

export interface ValuationJob {
	runId: string;
	mode: ValuationJobMode;
	status: ValuationJobStatus;
	total: number;
	done: number;
	failed: number;
	currentItem: string | null;
	error: string | null;
	startedAt: string;
	finishedAt: string | null;
}

interface JobResponse {
	job: ValuationJob | null;
}

export const valuations = {
	list: (signal?: AbortSignal) =>
		request<{
			items: ValuedItem[];
			job: ValuationJob | null;
			fieldName: string;
			currency: string;
		}>('/valuations', { signal, timeout: 0 }),

	/** Write approved estimates to Homebox; only the value custom field changes. */
	apply: (itemIds: string[]) =>
		request<ApplyResult>('/valuations/apply', {
			method: 'POST',
			body: JSON.stringify({ itemIds }),
			timeout: 0,
		}),

	/** Drop pending estimates without touching Homebox. */
	discard: (itemIds: string[]) =>
		request<{ discarded: number }>('/valuations/discard', {
			method: 'POST',
			body: JSON.stringify({ itemIds }),
		}),

	getJob: (signal?: AbortSignal) => request<JobResponse>('/valuations/job', { signal }),

	start: (mode: ValuationJobMode) =>
		request<JobResponse>('/valuations/job', { method: 'POST', body: JSON.stringify({ mode }) }),

	resume: () => request<JobResponse>('/valuations/job/resume', { method: 'POST' }),

	cancel: () => request<JobResponse>('/valuations/job/cancel', { method: 'POST' }),
};
