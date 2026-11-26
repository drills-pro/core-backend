import { useInfiniteQuery, type QueryKey } from '@tanstack/react-query';

/** The shape of every keyset-paged core-api list: one page of items and the token of the next. */
export type Page<T> = { items: T[]; next_page_token?: string | null };

/** A keyset-paged list read as one: the pages fetched so far, flattened, and a way to fetch the next. Changing the key
 * (a filter, say) starts over from the first page. */
export function usePagedList<T>(queryKey: QueryKey, fetchPage: (pageToken?: string) => Promise<Page<T>>) {
  const q = useInfiniteQuery({
    queryKey,
    queryFn: ({ pageParam }) => fetchPage(pageParam),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (last) => last.next_page_token ?? undefined,
  });
  return {
    items: q.data?.pages.flatMap((p) => p.items) ?? [],
    error: q.error,
    isPending: q.isPending,
    hasMore: q.hasNextPage,
    loadingMore: q.isFetchingNextPage,
    loadMore: () => void q.fetchNextPage(),
  };
}
