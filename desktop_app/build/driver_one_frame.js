// Checks the single-frame claim: the app opens on the workbench, with no
// landing stage to scroll past and no vertical scrollbar on the page.
await new Promise((r) => setTimeout(r, 700)); // let the entrance transitions settle

const doc = document.documentElement;
const head = document.querySelector(".workbench-head");
const actions = document.querySelector(".anon-actions");
const steps = Array.from(document.querySelectorAll(".workflow-steps li"));

window.__frame = {
  landingStagePresent: !!document.querySelector(".landing-stage, .scroll-cue"),
  pageScrollsVertically: doc.scrollHeight > doc.clientHeight + 1,
  viewport: { w: doc.clientWidth, h: doc.clientHeight },
  headText: head ? head.innerText.replace(/\s+/g, " ").trim() : null,
  headHeight: head ? Math.round(head.getBoundingClientRect().height) : null,
  // The action row is the control that kept ending up below the fold.
  actionsBottom: actions ? Math.round(actions.getBoundingClientRect().bottom) : null,
  actionsInView: actions
    ? actions.getBoundingClientRect().bottom <= doc.clientHeight + 1
    : null,
  stepsVisible: steps.length,
  stepsPreHighlighted: steps.filter((li) => li.className.includes("step-")).length,
  revealedOpacity: head ? getComputedStyle(head).opacity : null,
  // "In view" is only meaningful alongside this: if the card scrolls
  // internally, Approve is reachable but not on screen unscrolled.
  toolCardScrolls: (() => {
    const c = document.querySelector(".anon-tool-card");
    return c ? c.scrollHeight > c.clientHeight + 1 : null;
  })(),
};

// And the stepper must still respond to progress.
setStep(2);
window.__frame.afterSetStep2 = steps.map((li) => li.id + ":" + (li.className || "none"));
setStep(1);
