export function ForkIcon() {
  return (
    <svg
      aria-hidden="true"
      className="action-icon"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeLinecap="round"
      strokeLinejoin="round"
      strokeWidth="1.8"
    >
      <circle cx="6" cy="5" r="2.25" />
      <circle cx="18" cy="5" r="2.25" />
      <circle cx="12" cy="19" r="2.25" />
      <path d="M6 7.25v2.5A7.25 7.25 0 0 0 12 17" />
      <path d="M18 7.25v2.5A7.25 7.25 0 0 1 12 17" />
    </svg>
  );
}

export function TrashIcon() {
  return (
    <svg
      aria-hidden="true"
      className="action-icon"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeLinecap="round"
      strokeLinejoin="round"
      strokeWidth="1.8"
    >
      <path d="M4 7h16" />
      <path d="M9 7V4.75A1.75 1.75 0 0 1 10.75 3h2.5A1.75 1.75 0 0 1 15 4.75V7" />
      <path d="m6.25 7 .75 12a2 2 0 0 0 2 1.88h6a2 2 0 0 0 2-1.88l.75-12" />
      <path d="M10 11v5.5M14 11v5.5" />
    </svg>
  );
}
