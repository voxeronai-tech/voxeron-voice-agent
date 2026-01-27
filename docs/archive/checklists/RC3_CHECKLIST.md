# RC3 Close-out Checklist (Deterministic Demo)

## Must-pass (blockers)
- [ ] No crash: UnboundLocalError "re" in process_utterance
- [ ] Cart-check: accept "Yes, …" and process remainder (no loop)
- [ ] Cart-check: accept "No, …" and process remainder as change request
- [ ] Cart-check: if user says a change request instead of yes/no, treat as NEGATE + fall through
- [ ] Naan math: "make it 3 naan" sets qty (2 -> 3), never accumulates (2 -> 5)
- [ ] Mixed naan: "two plain and one garlic" sets both correctly
- [ ] Language stability: no auto-switch; only explicit language commands

## Manual test scripts to run after each relevant commit
A) Happy flow
1. Indian food -> 2 butter chicken + 2 naan -> plain -> yes -> pickup -> name -> that's all

B) Confirm + change during confirm
1. … -> Agent asks "Is that correct?"
2. User: "No, make it three naan"

C) Mixed naan change
1. … -> "Instead of three naan, two plain and one garlic"

D) Offer accept
1. Ask "spiciest lamb"
2. User: "Yes please" (should add offered item)
