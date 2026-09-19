import { ImageResponse } from "next/og";

export const alt = "Outreach — Personalized email outreach made simple";
export const size = { width: 1200, height: 630 };
export const contentType = "image/png";

export default function OpenGraphImage() {
  return new ImageResponse(
    (
      <div
        style={{
          alignItems: "center",
          background: "#f8fafc",
          color: "#0f172a",
          display: "flex",
          flexDirection: "column",
          height: "100%",
          justifyContent: "center",
          padding: "72px",
          width: "100%",
        }}
      >
        <div
          style={{
            alignItems: "center",
            color: "#2563eb",
            display: "flex",
            fontSize: 34,
            fontWeight: 700,
            letterSpacing: "0.16em",
            marginBottom: 38,
          }}
        >
          OUTREACH
        </div>
        <div
          style={{
            display: "flex",
            fontSize: 76,
            fontWeight: 700,
            letterSpacing: "-0.055em",
            lineHeight: 1.03,
            maxWidth: 980,
            textAlign: "center",
          }}
        >
          Write one email. Make it personal for everyone.
        </div>
        <div
          style={{
            color: "#475569",
            display: "flex",
            fontSize: 30,
            marginTop: 42,
            textAlign: "center",
          }}
        >
          Personalized email outreach, from one simple workspace.
        </div>
      </div>
    ),
    { ...size },
  );
}
