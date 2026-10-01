import type { Metadata } from "next";
import { SITE_URL } from "@/lib/site";

export const metadata: Metadata = {
  title: "Connect an AI agent",
  description:
    "How to connect an AI agent to Outreach over the Model Context Protocol, including the endpoint, OAuth sign-in, and required scope.",
  alternates: { canonical: "/mcp-guide" },
};

const ENDPOINT = `${SITE_URL}/mcp`;
const CARD = `${SITE_URL}/.well-known/mcp.json`;
const LLMS = `${SITE_URL}/llms.txt`;

function Code({ children }: { children: React.ReactNode }) {
  return (
    <code className="rounded bg-slate-100 px-1.5 py-0.5 font-mono text-[0.85em] text-slate-800 ring-1 ring-slate-200">
      {children}
    </code>
  );
}

function Block({ children }: { children: React.ReactNode }) {
  return (
    <pre className="mt-3 overflow-x-auto rounded-lg bg-slate-900 p-4 text-sm leading-relaxed text-slate-100">
      <code>{children}</code>
    </pre>
  );
}

export default function McpGuidePage() {
  return (
    <article className="mx-auto w-full max-w-3xl px-6 py-14 text-slate-700">
      <header className="border-b border-slate-200 pb-8">
        <p className="text-sm font-medium tracking-wide text-slate-500 uppercase">
          For AI agents
        </p>
        <h1 className="mt-2 text-3xl font-semibold tracking-tight text-slate-900">
          Connect an agent to Outreach
        </h1>
        <p className="mt-4 text-lg text-slate-600">
          Outreach runs a remote Model Context Protocol server. An agent that
          connects with it can manage campaigns, audiences, senders, contacts,
          templates and sending limits on your behalf.
        </p>
      </header>

      <section className="mt-10">
        <h2 className="text-xl font-semibold text-slate-900">Connection details</h2>
        <dl className="mt-4 grid gap-x-6 gap-y-3 sm:grid-cols-[10rem_1fr]">
          <dt className="text-slate-500">Endpoint</dt>
          <dd>
            <Code>{ENDPOINT}</Code>
          </dd>
          <dt className="text-slate-500">Transport</dt>
          <dd>Streamable HTTP, stateless (no session to keep)</dd>
          <dt className="text-slate-500">Authentication</dt>
          <dd>OAuth 2.0 authorization code with PKCE</dd>
          <dt className="text-slate-500">Required scope</dt>
          <dd>
            <Code>outreach:manage</Code>
          </dd>
          <dt className="text-slate-500">Machine-readable guide</dt>
          <dd>
            <a className="underline" href={LLMS}>
              {LLMS}
            </a>
          </dd>
        </dl>
      </section>

      <section className="mt-10">
        <h2 className="text-xl font-semibold text-slate-900">Four steps</h2>
        <ol className="mt-4 space-y-6">
          <li>
            <h3 className="font-medium text-slate-900">1. Discover</h3>
            <p className="mt-1">
              The server card names the transport and the endpoint, so a path
              never has to be guessed.
            </p>
            <Block>{`GET ${CARD}`}</Block>
          </li>
          <li>
            <h3 className="font-medium text-slate-900">2. Register</h3>
            <p className="mt-1">
              Read <Code>registration_endpoint</Code> from{" "}
              <Code>{SITE_URL}/.well-known/oauth-authorization-server</Code> and
              register there. There is no client secret to store: a public client
              cannot keep one.
            </p>
          </li>
          <li>
            <h3 className="font-medium text-slate-900">3. Authorize</h3>
            <p className="mt-1">
              Send the account owner to the authorization endpoint. They sign in
              to Outreach and approve <Code>outreach:manage</Code>. No password or
              token is ever shared with the agent.
            </p>
          </li>
          <li>
            <h3 className="font-medium text-slate-900">4. Call</h3>
            <p className="mt-1">
              Exchange the code plus the PKCE verifier at the token endpoint, then
              send the access token as a bearer credential.
            </p>
            <Block>{`POST ${ENDPOINT}
Authorization: Bearer <access_token>
Content-Type: application/json`}</Block>
          </li>
        </ol>
      </section>

      <section className="mt-10 rounded-xl bg-slate-50 p-6 ring-1 ring-slate-200">
        <h2 className="text-xl font-semibold text-slate-900">
          If a step is unclear
        </h2>
        <p className="mt-2">
          Call the endpoint with no credentials and read the{" "}
          <Code>WWW-Authenticate</Code> response header. It states the required
          scope and the exact metadata URL to fetch next.
        </p>
        <Block>{`POST ${ENDPOINT}   →   401
WWW-Authenticate: Bearer ..., scope="outreach:manage",
  resource_metadata="${SITE_URL}/.well-known/oauth-protected-resource/mcp"`}</Block>
      </section>

      <section className="mt-10">
        <h2 className="text-xl font-semibold text-slate-900">Common mistakes</h2>
        <ul className="mt-4 list-disc space-y-2 pl-5">
          <li>
            <strong>Looking for an API key or a token file.</strong> The hosted
            server has neither. It is OAuth only.
          </li>
          <li>
            <strong>Treating the 401 as a failure.</strong> It is the intended
            answer and carries the discovery information.
          </li>
          <li>
            <strong>Storing the client secret.</strong> Registration returns a
            public client with no secret.
          </li>
          <li>
            <strong>Guessing the scope.</strong> Use <Code>outreach:manage</Code>.
            Omitting the scope parameter also works, because it is a registered
            default scope.
          </li>
          <li>
            <strong>Dropping the <Code>www</Code>.</strong> Use{" "}
            <Code>{SITE_URL}</Code> exactly; that is the registered origin.
          </li>
        </ul>
      </section>

      <section className="mt-10">
        <h2 className="text-xl font-semibold text-slate-900">What the access allows</h2>
        <p className="mt-2">
          A connected agent can read and change campaigns, audiences, senders,
          contacts, templates and settings, and can send email. It cannot
          administer global Gmail OAuth credentials or mint access tokens. Treat
          an approved agent as equivalent to a signed-in browser session, and
          revoke it in Clerk under the account&rsquo;s connected applications when
          it is no longer needed.
        </p>
      </section>
    </article>
  );
}
