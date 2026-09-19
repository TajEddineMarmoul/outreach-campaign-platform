"use client";

import { useEffect, useState } from "react";
import { useAuth } from "@clerk/nextjs";
import { useRouter } from "next/navigation";
import { ArrowRight, CheckCheck, Mail, Users } from "lucide-react";
import { AppDialog } from "@/components/app-ui";
import { trackEventOnce } from "@/lib/analytics";
import { completeWelcome, showCampaignTips, hideCampaignTips } from "@/lib/onboarding";
import "./onboarding.css";

const steps = [
  { title: "Choose your audience", text: "Import the people you want to reach.", icon: Users },
  { title: "Write your message", text: "Write once. Personalize for each person.", icon: Mail },
  { title: "Review and launch", text: "Choose a sender and schedule, then confirm.", icon: CheckCheck },
];

export default function WelcomePage() {
  const { userId } = useAuth();
  const router = useRouter();
  const [sampleOpen, setSampleOpen] = useState(false);
  const [guide, setGuide] = useState(true);

  useEffect(() => {
    if (!userId) return;
    trackEventOnce(`${userId}:sign-up-complete`, "sign_up_complete");
  }, [userId]);

  const leaveWelcome = (create: boolean) => {
    if (!userId) return;
    completeWelcome(userId);
    if (create && guide) showCampaignTips(userId);
    else hideCampaignTips(userId);
    router.push(create ? "/campaigns?create=1" : "/campaigns");
  };

  return (
    <section className="welcome-page" aria-labelledby="welcome-title">
      <div className="welcome-content">
        <p className="welcome-eyebrow">Welcome to Outreach</p>
        <h1 id="welcome-title">Send your first campaign<br className="welcome-break" /> with confidence</h1>
        <p className="welcome-intro">A campaign brings together the people you want to email, your message, and when it should send.</p>
        <p className="welcome-assurance">Nothing sends until you review and launch.</p>

        <ol className="welcome-steps">
          {steps.map(({ title, text, icon: Icon }, index) => (
            <li key={title}>
              <div className="welcome-step-symbol"><span>{index + 1}</span><Icon size={25} strokeWidth={1.5} aria-hidden="true" /></div>
              <h2>{title}</h2>
              <p>{text}</p>
            </li>
          ))}
        </ol>

        <div className="welcome-actions">
          <button className="app-button is-primary" onClick={() => leaveWelcome(true)} disabled={!userId}>Create my first campaign <ArrowRight size={18} /></button>
          <button className="app-button is-quiet" onClick={() => setSampleOpen(true)}>Explore a sample</button>
        </div>
        <label className="welcome-guide-choice"><input type="checkbox" checked={guide} onChange={(event) => setGuide(event.target.checked)} /> Show a few tips as I get started</label>
        <p className="welcome-sample-note">The sample uses fictional contacts and never sends email.</p>
        <button className="welcome-skip" onClick={() => leaveWelcome(false)} disabled={!userId}>Skip for now</button>
      </div>

      <AppDialog open={sampleOpen} onClose={() => setSampleOpen(false)} title="A campaign, from start to finish" description="This is a fictional example. It is not saved to your account and cannot send email.">
        <ol className="welcome-sample">
          <li><span>1</span><div><h3>Audience</h3><p>Import alex@example.com and sam@example.com, with any extra fields you need.</p></div></li>
          <li><span>2</span><div><h3>Message</h3><p>Write a subject and email. Preview lets you see how the message will look to each person.</p></div></li>
          <li><span>3</span><div><h3>Review and launch</h3><p>Choose your sending account and timing. Nothing sends until you confirm on Review.</p></div></li>
        </ol>
        <div className="app-dialog-actions"><button className="app-button is-primary" onClick={() => setSampleOpen(false)}>Got it</button></div>
      </AppDialog>
    </section>
  );
}
