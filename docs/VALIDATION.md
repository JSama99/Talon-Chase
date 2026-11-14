# Validation kit

The engine was built before talking to brokers. These are the three questions that
decide whether to keep going, and the materials to answer them.

| Question | Go if | Stop or rethink if |
| --- | --- | --- |
| Is the POD chase big enough? | 30% or more of loads have paperwork arriving over 24 hours after delivery, and the owner raises it unprompted | Under 15%, or drivers already upload through an app on most loads |
| Can we write to their TMS? | At least two common TMSs allow API or automated filing | Only the emailed packet works anywhere |
| Will carriers answer a machine? | 60% or more of PODs arrive with no human touch; median under 24 hours from delivery to checked POD | Under 30%, or carriers complain |

## Interview script (20 minutes)

Talk to the owner or head of operations at a brokerage with 5 to 50 people. Ask them
to have last month's load list open.

1. Walk me through yesterday's delivered loads. Which have paperwork now?
2. Who asks for the POD, how, and how many times on a bad one?
3. Pick last month. How many loads delivered? How many had the POD more than a day late?
4. How many days from delivery to invoice, typically? What is one day faster worth to you?
5. Which TMS and plan are you on? Does it have AI features? Do you use them? Why not?
6. Do carriers upload documents into your TMS themselves? What share?
7. Has a carrier email ever turned out to be fake? What happened?
8. Would you set up a `pods@` mailbox and add a text-consent line to your rate confirmation?
9. If this cost $1.50 a delivered load, would you start next month? If not, what number or proof changes that?
10. Who else should I talk to?

Do not describe the product before question 8.

## Counting sheet

One row per interview.

| Field | Value |
| --- | --- |
| Company, role of the person, date | |
| Staff count | |
| Loads delivered last month | |
| Loads with POD over 24 hours late | |
| Late share (computed) | |
| Typical days, delivery to invoice | |
| Who chases, hours a week (their estimate) | |
| TMS and plan | |
| Uses the TMS's AI features (yes, no, did not know) | |
| Share of PODs uploaded by carriers already | |
| Fraud incident (yes, no) and what | |
| Would create mailbox and consent line (yes, no) | |
| Reaction to $1.50 a load, in their words | |
| Would pilot (yes, no, maybe) | |

## Letter to TMS vendors

Send to AscendTMS, Descartes Aljex, Tai Software and Rose Rocket. Fill in the brackets.

> Subject: API access for document upload on behalf of a shared customer
>
> Hello,
>
> I am building a tool that collects signed proofs of delivery from carriers for
> freight brokers and files them on the load. Several brokers I am speaking with use
> [TMS name], and I want to file documents into it the supported way.
>
> Could you tell me:
>
> 1. Is there an API a broker can authorise for a third party? On which plans, and at
>    what cost?
> 2. Can it list loads by status, attach a document to a load, and set a delivered or
>    ready-to-invoice status? Are there webhooks for status changes?
> 3. Is there a document inbox or carrier portal address that files a document when it
>    includes a load number?
> 4. Do your terms allow an automated user account, created by the broker, to attach
>    documents through the normal screens?
> 5. Is there a partner or integration programme, and who should I speak to?
>
> Thank you,
> [name, company, phone]

## Pilot

Two brokers, two weeks, emailed-packet filing only, a person approving each send in
week one. Before starting, the outbox and a real mailbox adapter must exist (see the
README), and the text-consent wording should be reviewed by a lawyer.

Report at the end:

- Share of PODs collected with no human touch
- Median hours from delivery to checked POD
- Documents misfiled (target: zero)
- Exceptions caught
- Carrier complaints
- Cost per load
- Whether each broker agrees to pay
