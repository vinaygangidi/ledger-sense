import "./globals.css";

export const metadata = {
  title: "AR Reconciliation Copilot",
  description: "Auditable AR reconciliation with GPT-5.6",
};

export default function RootLayout({ children }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
