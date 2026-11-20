import { PRODUCT } from "@/lib/product";

/** The product's mark, as on the site: a wave that settles into an "=" (the app's sign for proved). Only drawn. */
function Mark() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <path d="M3 10.95C5.6 5.75 9.6 5.75 12 10.95H21M3 16.95H21" fill="none" stroke="currentColor" strokeWidth="3.1" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

/** The same wordmark on sign-in, installation and the desktop shell. */
export function Brand() {
  return <p className="wm mt-0 mr-0 mb-4 ml-0.5 inline-flex shrink-0 items-center gap-2 text-lg font-[650] lowercase tracking-tight [&_svg]:size-6 [&_svg]:text-brand"><Mark />{PRODUCT}</p>;
}
