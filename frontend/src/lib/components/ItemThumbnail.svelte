<script lang="ts">
	/**
	 * Item thumbnail that loads only when scrolled into view.
	 * Used in long item tables where loading every image up front is too heavy.
	 */
	import { Package } from '@lucide/svelte';
	import { items as itemsApi, type BlobUrlResult } from '$lib/api/items';

	interface Props {
		itemId: string;
		thumbnailId: string | null;
		alt: string;
	}

	let { itemId, thumbnailId, alt }: Props = $props();

	let container: HTMLDivElement | null = $state(null);
	let url = $state<string | null>(null);

	$effect(() => {
		if (!container || !thumbnailId) return;
		const id = thumbnailId;
		const controller = new AbortController();
		let result: BlobUrlResult | null = null;

		const observer = new IntersectionObserver(
			(entries) => {
				if (!entries.some((entry) => entry.isIntersecting)) return;
				observer.disconnect();
				itemsApi
					.getThumbnail(itemId, id, controller.signal)
					.then((loaded) => {
						if (controller.signal.aborted) {
							loaded.revoke();
							return;
						}
						result = loaded;
						url = loaded.url;
					})
					.catch(() => {
						// Missing thumbnails are not critical: keep the placeholder
					});
			},
			{ rootMargin: '200px' }
		);
		observer.observe(container);

		return () => {
			observer.disconnect();
			controller.abort();
			result?.revoke();
			url = null;
		};
	});
</script>

<div
	bind:this={container}
	class="flex size-12 shrink-0 items-center justify-center overflow-hidden rounded-lg bg-neutral-800"
>
	{#if url}
		<img src={url} {alt} class="size-full object-cover" />
	{:else}
		<Package size={20} strokeWidth={1.5} class="text-neutral-500" />
	{/if}
</div>
