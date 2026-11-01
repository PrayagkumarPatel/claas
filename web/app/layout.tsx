import type { Metadata } from "next"
import "./globals.css"

export const metadata: Metadata = {
  title: "CLaaS Terminal — GPU Compute Spot Market",
  description:
    "Live order book and trade tape for the CLaaS GPU compute marketplace. Bids, asks, and executed trades in real time.",
}

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    // dark mode is the default (and only) theme for the terminal
    <html lang="en" className="dark">
      <body className="min-h-screen bg-background font-sans text-foreground antialiased">
        {children}
      </body>
    </html>
  )
}
