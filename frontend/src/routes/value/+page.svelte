<script lang="ts">
	/**
	 * Value page - used-market value of every item.
	 *
	 * Homebox is the source of truth: the value lives in a custom field of each
	 * item. Valuations run as a server-side background job (it can take days)
	 * and produce estimates that must be approved before they are written to
	 * Homebox.
	 */
	import { goto } from '$app/navigation';
	import { resolve } from '$app/paths';
	import { onMount } from 'svelte';
	import { SvelteSet } from 'svelte/reactivity';
	import {
		AlertTriangle,
		Check,
		ExternalLink,
		RefreshCw,
		Search,
		TrendingUp,
		X,
	} from '@lucide/svelte';
	import { authStore } from '$lib/stores/auth.svelte';
	import { showToast } from '$lib/stores/ui.svelte';
	import { getInitPromise } from '$lib/services/bootstrap';
	import { valuations, type ValuationJob, type ValuedItem } from '$lib/api/valuations';
	import { createLogger } from '$lib/utils/logger';
	import Button from '$lib/components/Button.svelte';
	import ConfirmDialog from '$lib/components/ConfirmDialog.svelte';
	import ItemThumbnail from '$lib/components/ItemThumbnail.svelte';

	const log = createLogger({ prefix: 'ValuePage' });

	const POLL_INTERVAL_MS = 5000;
	const BIG_INCREASE_PCT = 50;

	let rows = $state<ValuedItem[]>([]);
	let job = $state<ValuationJob | null>(null);
	let fieldName = $state('');
	let isLoading = $state(true);
	let loadError = $state<string | null>(null);
	let isStarting = $state(false);
	let isWriting = $state(false);
	let confirmWrite = $state(false);
	let expanded = new SvelteSet<string>();
	let selected = new SvelteSet<string>();

	const isActive = $derived(job?.status === 'snapshot' || job?.status === 'running');
	const canResume = $derived(
		!!job &&
			['interrupted', 'cancelled', 'failed'].includes(job.status) &&
			job.total > job.done + job.failed
	);
	const inHomebox = $derived(
		rows
			.filter((row) => row.homeboxValue)
			.sort((a, b) => b.homeboxValue!.value - a.homeboxValue!.value)
	);
	const withoutValue = $derived(rows.filter((row) => !row.homeboxValue));
	const missingCount = $derived(withoutValue.filter((row) => !row.pending).length);
	// Estimates awaiting approval: big increases first, then by change, new values last
	const pendingRows = $derived(
		rows
			.filter((row) => row.pending)
			.sort((a, b) => {
				const bigA = isBigIncrease(a) ? 1 : 0;
				const bigB = isBigIncrease(b) ? 1 : 0;
				return (
					bigB - bigA ||
					(b.changePct ?? -Infinity) - (a.changePct ?? -Infinity) ||
					b.estimate!.value - a.estimate!.value
				);
			})
	);
	const selectedPending = $derived(pendingRows.filter((row) => selected.has(row.id)));
	const allSelected = $derived(
		pendingRows.length > 0 && selectedPending.length === pendingRows.length
	);
	const totalsByCurrency = $derived.by(() => {
		const totals: Record<string, number> = {};
		for (const row of inHomebox) {
			const currency = row.homeboxValue!.currency ?? '';
			totals[currency] = (totals[currency] ?? 0) + row.homeboxValue!.value;
		}
		return Object.entries(totals);
	});

	function isBigIncrease(row: ValuedItem): boolean {
		return (row.changePct ?? 0) > BIG_INCREASE_PCT;
	}

	function formatMoney(value: number | null | undefined, currency: string | null): string {
		if (value === null || value === undefined) return '—';
		if (!currency) return value.toFixed(2);
		try {
			return new Intl.NumberFormat(undefined, { style: 'currency', currency }).format(value);
		} catch {
			return `${value.toFixed(2)} ${currency}`;
		}
	}

	function formatChange(pct: number | null): string {
		if (pct === null) return 'new';
		return `${pct > 0 ? '+' : ''}${pct.toFixed(1)}%`;
	}

	function toggle(set: SvelteSet<string>, id: string) {
		if (set.has(id)) set.delete(id);
		else set.add(id);
	}

	function toggleAll() {
		if (allSelected) selected.clear();
		else for (const row of pendingRows) selected.add(row.id);
	}

	async function loadAll() {
		try {
			const response = await valuations.list();
			rows = response.items;
			job = response.job;
			fieldName = response.fieldName;
			loadError = null;
			// Forget selections of estimates that are no longer pending
			const pendingIds = new Set(rows.filter((row) => row.pending).map((row) => row.id));
			for (const id of [...selected]) if (!pendingIds.has(id)) selected.delete(id);
		} catch (error) {
			log.error('Failed to load valuations', error);
			loadError = error instanceof Error ? error.message : 'Failed to load valuations';
		} finally {
			isLoading = false;
		}
	}

	async function pollJob() {
		const wasActive = isActive;
		const previousDone = (job?.done ?? 0) + (job?.failed ?? 0);
		try {
			job = (await valuations.getJob()).job;
		} catch (error) {
			log.debug('Job poll failed', error);
			return;
		}
		const nowDone = (job?.done ?? 0) + (job?.failed ?? 0);
		// Reload the table when items finish so results appear progressively
		if (nowDone !== previousDone || (wasActive && !isActive)) await loadAll();
	}

	async function start(mode: 'missing' | 'refresh') {
		isStarting = true;
		try {
			job = (await valuations.start(mode)).job;
			showToast('Valuation started. You can leave this page: it runs on the server.', 'info');
		} catch (error) {
			showToast(error instanceof Error ? error.message : 'Failed to start valuation', 'error');
		} finally {
			isStarting = false;
		}
	}

	async function resume() {
		try {
			job = (await valuations.resume()).job;
		} catch (error) {
			showToast(error instanceof Error ? error.message : 'Failed to resume valuation', 'error');
		}
	}

	async function cancel() {
		try {
			job = (await valuations.cancel()).job;
			await loadAll();
		} catch (error) {
			showToast(error instanceof Error ? error.message : 'Failed to stop valuation', 'error');
		}
	}

	async function writeSelected() {
		confirmWrite = false;
		const ids = selectedPending.map((row) => row.id);
		if (ids.length === 0) return;
		isWriting = true;
		try {
			const result = await valuations.apply(ids);
			const failed = Object.keys(result.errors).length;
			if (failed === 0) {
				showToast(`${result.applied.length} value(s) written to Homebox`, 'success');
			} else {
				const firstError = Object.values(result.errors)[0];
				showToast(
					`${result.applied.length} written, ${failed} not written (${firstError})`,
					'warning'
				);
			}
		} catch (error) {
			showToast(error instanceof Error ? error.message : 'Failed to write values', 'error');
		} finally {
			isWriting = false;
			await loadAll();
		}
	}

	async function discardSelected() {
		const ids = selectedPending.map((row) => row.id);
		if (ids.length === 0) return;
		try {
			const { discarded } = await valuations.discard(ids);
			showToast(`${discarded} estimate(s) discarded`, 'info');
		} catch (error) {
			showToast(error instanceof Error ? error.message : 'Failed to discard estimates', 'error');
		} finally {
			await loadAll();
		}
	}

	onMount(() => {
		let disposed = false;
		let timer: ReturnType<typeof setInterval> | undefined;
		void initialize();
		return () => {
			disposed = true;
			clearInterval(timer);
		};

		async function initialize() {
			await getInitPromise();
			if (disposed) return;
			if (!authStore.isAuthenticated) {
				goto(resolve('/'));
				return;
			}
			await loadAll();
			if (disposed) return;
			timer = setInterval(() => {
				if (isActive) void pollJob();
			}, POLL_INTERVAL_MS);
		}
	});

	const STATUS_LABELS: Record<ValuationJob['status'], string> = {
		snapshot: 'Reading items from Homebox…',
		running: 'Searching the web and estimating values…',
		completed: 'Completed',
		cancelled: 'Stopped',
		interrupted: 'Interrupted (server restarted)',
		failed: 'Failed',
	};
</script>

<svelte:head>
	<title>Value - Homebox Companion</title>
</svelte:head>

{#snippet estimateDetails(row: ValuedItem)}
	{#if row.estimate}
		{@const estimate = row.estimate}
		<div class="space-y-2 text-body-sm text-neutral-400">
			<p>
				Estimate {formatMoney(estimate.value, estimate.currency)} · range {formatMoney(
					estimate.low,
					estimate.currency
				)} – {formatMoney(estimate.high, estimate.currency)} · confidence {estimate.confidence}
				· {new Date(estimate.valuedAt).toLocaleDateString()}
				{row.pending ? '· awaiting approval' : ''}
			</p>
			{#if estimate.reasoning}
				<p>{estimate.reasoning}</p>
			{/if}
			{#if estimate.sources.length}
				<ul class="space-y-1">
					{#each estimate.sources as source (source.url)}
						<li>
							<!-- eslint-disable svelte/no-navigation-without-resolve -- External URL, not an app route -->
							<a
								href={source.url}
								target="_blank"
								rel="noopener noreferrer"
								class="inline-flex items-center gap-1 text-primary-400 hover:text-primary-300"
							>
								<ExternalLink size={12} />
								<span class="line-clamp-1">{source.title || source.url}</span>
							</a>
							<!-- eslint-enable svelte/no-navigation-without-resolve -->
							{#if source.price}<span class="text-neutral-500"> · {source.price}</span>{/if}
						</li>
					{/each}
				</ul>
			{/if}
		</div>
	{/if}
	{#if row.lastError}
		<p class="mt-2 text-body-sm text-warning-500">Last attempt: {row.lastError}</p>
	{/if}
	{#if !row.estimate && !row.lastError}
		<p class="text-body-sm text-neutral-400">No estimate.</p>
	{/if}
{/snippet}

<div class="animate-in space-y-6">
	<div>
		<h1 class="text-h1 font-bold text-neutral-100">Value</h1>
		<p class="mt-1 text-body-sm text-neutral-400">
			Estimated used-market value from similar second-hand listings. Estimates are written to
			Homebox, in the “{fieldName || '…'}” custom field, only after you approve them.
		</p>
	</div>

	{#if loadError}
		<div class="card border-error-500/30 bg-error-500/10">
			<div class="flex items-start gap-3">
				<AlertTriangle class="mt-0.5 shrink-0 text-error-500" size={20} strokeWidth={1.5} />
				<div>
					<p class="font-medium text-error-500">Failed to load valuations</p>
					<p class="mt-1 text-body-sm text-neutral-400">{loadError}</p>
					<button
						type="button"
						class="mt-2 text-body-sm text-primary-400 underline hover:text-primary-300"
						onclick={loadAll}
					>
						Try again
					</button>
				</div>
			</div>
		</div>
	{/if}

	<div class="card space-y-4">
		<div class="flex flex-wrap items-baseline justify-between gap-2">
			<div>
				<p class="text-body-sm text-neutral-400">Total value in Homebox</p>
				<p class="text-h2 font-bold text-neutral-100">
					{#if totalsByCurrency.length}
						{totalsByCurrency
							.map(([currency, total]) => formatMoney(total, currency || null))
							.join(' + ')}
					{:else}
						—
					{/if}
				</p>
			</div>
			<p class="text-body-sm text-neutral-400">
				{inHomebox.length} with value · {withoutValue.length} without · {pendingRows.length} to approve
			</p>
		</div>

		<div class="flex flex-col gap-2 sm:flex-row">
			<Button
				full
				disabled={isActive || isLoading || missingCount === 0}
				loading={isStarting}
				onclick={() => start('missing')}
			>
				<Search size={16} />
				Value items without a value ({missingCount})
			</Button>
			<Button
				full
				variant="secondary"
				disabled={isActive || isLoading || inHomebox.length === 0}
				loading={isStarting}
				onclick={() => start('refresh')}
			>
				<RefreshCw size={16} />
				Refresh all values ({inHomebox.length})
			</Button>
		</div>
		<p class="text-caption text-neutral-500">
			Each item can take several minutes. The job runs on the server: you can close this page and
			come back later. Nothing is written to Homebox until you approve it below.
		</p>
	</div>

	{#if job}
		{@const handled = job.done + job.failed}
		<div class="card space-y-3" aria-live="polite">
			<div class="flex flex-wrap items-center justify-between gap-2">
				<p class="font-medium text-neutral-100">
					{job.mode === 'refresh' ? 'Refresh' : 'Valuation'}: {STATUS_LABELS[job.status]}
				</p>
				{#if isActive}
					<Button size="sm" variant="ghost" onclick={cancel}>Stop</Button>
				{:else if canResume}
					<Button size="sm" variant="secondary" onclick={resume}>Resume</Button>
				{/if}
			</div>
			{#if job.total > 0}
				<div class="h-2 overflow-hidden rounded-full bg-neutral-800">
					<div
						class="h-full bg-primary-500 transition-all"
						style="width: {Math.round((handled / job.total) * 100)}%"
					></div>
				</div>
				<p class="text-body-sm text-neutral-400">
					{handled} / {job.total} items · {job.done} estimated · {job.failed} without result
				</p>
			{/if}
			{#if isActive && job.currentItem}
				<p class="text-body-sm text-neutral-400">Now: {job.currentItem}</p>
			{/if}
			{#if job.error}
				<p class="text-body-sm text-error-500">{job.error}</p>
			{/if}
		</div>
	{/if}

	{#if pendingRows.length}
		<section class="space-y-2">
			<h2 class="flex items-center gap-2 text-h3 font-semibold text-neutral-100">
				<TrendingUp size={18} /> To approve ({pendingRows.length})
			</h2>
			<p class="text-body-sm text-neutral-400">
				Increases above {BIG_INCREASE_PCT}% are listed first and highlighted. Tap a row for the
				listings behind the estimate.
			</p>
			<div class="flex flex-col gap-2 sm:flex-row">
				<Button
					full
					disabled={selectedPending.length === 0 || isWriting}
					loading={isWriting}
					onclick={() => (confirmWrite = true)}
				>
					<Check size={16} />
					Write to Homebox ({selectedPending.length})
				</Button>
				<Button
					full
					variant="ghost"
					disabled={selectedPending.length === 0 || isWriting}
					onclick={discardSelected}
				>
					<X size={16} />
					Discard ({selectedPending.length})
				</Button>
			</div>
			<div class="card overflow-x-auto p-0">
				<table class="w-full text-left text-body-sm">
					<thead class="text-caption text-neutral-500">
						<tr>
							<th class="p-3">
								<input
									type="checkbox"
									checked={allSelected}
									onchange={toggleAll}
									aria-label="Select all"
									class="size-4 accent-primary-500"
								/>
							</th>
							<th class="p-3">Photo</th>
							<th class="p-3">Item</th>
							<th class="p-3 text-right">In Homebox</th>
							<th class="p-3 text-right">Estimate</th>
							<th class="p-3 text-right">Change</th>
						</tr>
					</thead>
					<tbody>
						{#each pendingRows as row (row.id)}
							{@const big = isBigIncrease(row)}
							<tr
								class="cursor-pointer border-t border-neutral-700 {big
									? 'bg-success-500/10'
									: 'hover:bg-neutral-800'}"
								data-big-increase={big || undefined}
								onclick={() => toggle(expanded, row.id)}
								aria-expanded={expanded.has(row.id)}
							>
								<td class="p-3">
									<input
										type="checkbox"
										checked={selected.has(row.id)}
										onclick={(event) => event.stopPropagation()}
										onchange={() => toggle(selected, row.id)}
										aria-label="Approve {row.name}"
										class="size-4 accent-primary-500"
									/>
								</td>
								<td class="p-3">
									<ItemThumbnail itemId={row.id} thumbnailId={row.thumbnailId} alt={row.name} />
								</td>
								<td class="p-3 font-medium text-neutral-100">{row.name}</td>
								<td class="p-3 text-right text-neutral-400">
									{row.homeboxValue
										? formatMoney(row.homeboxValue.value, row.homeboxValue.currency)
										: '—'}
								</td>
								<td class="p-3 text-right text-neutral-100">
									{formatMoney(row.estimate!.value, row.estimate!.currency)}
								</td>
								<td
									class="p-3 text-right font-semibold {big
										? 'text-success-400'
										: (row.changePct ?? 0) < 0
											? 'text-error-500'
											: 'text-neutral-300'}"
								>
									{formatChange(row.changePct)}
								</td>
							</tr>
							{#if expanded.has(row.id)}
								<tr class="bg-neutral-800/50">
									<td colspan="6" class="p-3">{@render estimateDetails(row)}</td>
								</tr>
							{/if}
						{/each}
					</tbody>
				</table>
			</div>
		</section>
	{/if}

	<section class="space-y-2">
		<h2 class="text-h3 font-semibold text-neutral-100">All items</h2>
		{#if isLoading}
			<p class="text-body-sm text-neutral-400">Loading items…</p>
		{:else if rows.length === 0}
			<p class="text-body-sm text-neutral-400">No items found.</p>
		{:else}
			<div class="card overflow-x-auto p-0">
				<table class="w-full text-left text-body-sm">
					<thead class="text-caption text-neutral-500">
						<tr>
							<th class="p-3">Photo</th>
							<th class="p-3">Item</th>
							<th class="p-3 text-right">Value in Homebox</th>
						</tr>
					</thead>
					<tbody>
						{#each [...inHomebox, ...withoutValue] as row (row.id)}
							<tr
								class="cursor-pointer border-t border-neutral-700 hover:bg-neutral-800"
								onclick={() => toggle(expanded, row.id)}
								aria-expanded={expanded.has(row.id)}
							>
								<td class="p-3">
									<ItemThumbnail itemId={row.id} thumbnailId={row.thumbnailId} alt={row.name} />
								</td>
								<td class="p-3 font-medium text-neutral-100">
									{row.name}
									{#if row.pending}
										<span class="block text-caption font-normal text-primary-400">
											Estimate to approve
										</span>
									{:else if !row.homeboxValue && row.lastError}
										<span class="block text-caption font-normal text-warning-500">
											No estimate found
										</span>
									{/if}
								</td>
								<td class="p-3 text-right font-semibold text-neutral-100">
									{row.homeboxValue
										? formatMoney(row.homeboxValue.value, row.homeboxValue.currency)
										: '—'}
								</td>
							</tr>
							{#if expanded.has(row.id)}
								<tr class="bg-neutral-800/50">
									<td colspan="3" class="p-3">{@render estimateDetails(row)}</td>
								</tr>
							{/if}
						{/each}
					</tbody>
				</table>
			</div>
		{/if}
	</section>

	<div class="h-4"></div>
</div>

<ConfirmDialog
	open={confirmWrite}
	title="Write values to Homebox?"
	message={`The estimated value of ${selectedPending.length} item(s) will be written to the “${fieldName}” custom field in Homebox. No other field is changed.`}
	confirmLabel="Write to Homebox"
	onConfirm={writeSelected}
	onCancel={() => (confirmWrite = false)}
/>
