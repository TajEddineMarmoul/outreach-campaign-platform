"use client";

import Link from "next/link";
import { useState } from "react";

type GettingStartedStoryProps = {
  startHref: string;
  startLabel: string;
  signInHref: string;
  signInLabel: string;
};

const questions = [
  {
    question: "Will anything send before I launch?",
    answer: <>No. Importing contacts and writing a message do not send email.<br />Sending starts only after you launch.</>,
  },
  {
    question: "Which email accounts are supported?",
    answer: "Outreach currently connects to Gmail and Google Workspace accounts.",
  },
  {
    question: "Can I use my own spreadsheet columns?",
    answer: "Yes. Keep an email column, then use your other columns to personalize each message.",
  },
  {
    question: "Can I save and reuse a message?",
    answer: "Yes. Save your message as a template and use it again whenever you need it.",
  },
  {
    question: "Is Outreach only for businesses?",
    answer: "No. Outreach works for individuals and teams who want to send thoughtful, personalized email.",
  },
  {
    question: "Can I pause sending?",
    answer: "Yes. You can pause future sends and continue when you are ready.",
  },
  {
    question: "Can an AI agent manage my campaigns?",
    answer: (
      <>
        Yes. Outreach runs an MCP server, so an assistant like Claude or Codex can
        work on your campaigns directly after you approve access once.{" "}
        <Link href="/mcp-guide">How to connect an agent</Link>.
      </>
    ),
  },
];

function Chevron() {
  return (
    <svg viewBox="0 0 24 14" aria-hidden="true">
      <path d="M2 2 12 12 22 2" />
    </svg>
  );
}

function BrandMark() {
  return (
    <svg viewBox="0 0 36 36" fill="currentColor" aria-hidden="true">
      <path d="M32.3 2.9 3.8 13.5c-2 .7-2 2.3-.1 2.9l10.4 3.5 3.5 10.5c.7 1.9 2.2 1.9 3-.1L33.2 4.5c.5-1.2.2-2-.9-1.6Z" />
    </svg>
  );
}

function FlyingPlane() {
  return (
    <svg className="getting-started-plane" viewBox="0 0 390 230" aria-hidden="true">
      <defs>
        <linearGradient id="getting-plane-top" x1="0" y1="0" x2="1" y2="1">
          <stop stopColor="#6fa6ff" />
          <stop offset="1" stopColor="#2d72f4" />
        </linearGradient>
        <linearGradient id="getting-plane-front" x1="0" y1="0" x2="1" y2="1">
          <stop stopColor="#1267f5" />
          <stop offset="1" stopColor="#0042ce" />
        </linearGradient>
      </defs>
      <path className="getting-started-flight-path" d="M20 210 C100 208 142 194 184 160 C202 145 204 116 184 112 C157 106 151 148 177 154 C213 162 247 115 277 75" />
      <g className="getting-started-plane-shape" transform="translate(245 8) rotate(-5 70 45)">
        <path d="M0 42 142 0 56 99 45 62Z" fill="url(#getting-plane-top)" />
        <path d="m45 62 97-62-72 75Z" fill="#0052e4" />
        <path d="m70 75 72-75-86 99Z" fill="url(#getting-plane-front)" />
      </g>
    </svg>
  );
}

export default function GettingStartedStory({
  startHref,
  startLabel,
  signInHref,
  signInLabel,
}: GettingStartedStoryProps) {
  const [openQuestion, setOpenQuestion] = useState<number | null>(0);

  return (
    <section id="getting-started" className="getting-started-story" aria-labelledby="getting-started-title">
      <div className="getting-started-faq">
        <div className="getting-started-intro">
          <div className="getting-started-question-mark" aria-hidden="true">?</div>
          <h2 id="getting-started-title">Before you<br />get started</h2>
          <p>A few things worth knowing.</p>
        </div>

        <div className="getting-started-questions">
          {questions.map(({ question, answer }, index) => (
            <div className="getting-started-question" key={question}>
              <button
                type="button"
                aria-expanded={openQuestion === index}
                aria-controls={`getting-started-answer-${index}`}
                onClick={() => setOpenQuestion((current) => current === index ? null : index)}
              >
                <span>{question}</span>
                <Chevron />
              </button>
              <p id={`getting-started-answer-${index}`} hidden={openQuestion !== index}>{answer}</p>
            </div>
          ))}
        </div>
      </div>

      <div className="getting-started-cta">
        <div className="getting-started-cta-copy">
          <h2>Your next email <span>starts here.</span></h2>
          <p>Add your contacts and write your first message.</p>
          <Link href={startHref}>{startLabel}</Link>
        </div>

        <FlyingPlane />

        <footer className="getting-started-footer">
          <Link className="getting-started-brand" href="/" aria-label="Outreach home">
            <BrandMark />
            <span>OUTREACH</span>
          </Link>
          <nav aria-label="Footer navigation">
            <Link href={signInHref}>{signInLabel}</Link>
            <a href="#product-main">Back to top</a>
          </nav>
        </footer>
      </div>
    </section>
  );
}
