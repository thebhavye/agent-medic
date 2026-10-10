"""Offline knowledge base for the research agent.

Each document opens with general context and keeps its key facts near the end, so
answering a question needs a full read_doc, not just the search snippet.
"""

DOCS = {
    "pilot-plant": {
        "title": "Halcyon pilot plant",
        "body": (
            "Halcyon Grid builds stationary storage for utilities that need to shift renewable "
            "output across hours rather than minutes. The pilot plant was the first site where the "
            "company ran full-size modules under real grid dispatch, with operators from two partner "
            "utilities rotating through the control room. Early months focused on commissioning, "
            "thermal tuning and the dispatch software, and the team published weekly availability "
            "numbers internally. The pilot plant is located in Narvik, Norway. In 2025 it reached "
            "412 MWh of annual delivered output, above the 350 MWh target set at commissioning."
        ),
    },
    "leadership": {
        "title": "Leadership team",
        "body": (
            "Halcyon Grid is run by a small executive team that came largely from utility operations "
            "and battery manufacturing. The company keeps engineering, manufacturing and field service "
            "under one reporting line so that problems found at customer sites reach the cell design "
            "team quickly. Board meetings are held quarterly, and the technical advisory group meets "
            "monthly to review reliability data from deployed systems. The chief executive is Tomas "
            "Brekke. The chief engineer is Ines Okafor, appointed in March 2024 after leading the "
            "cell qualification program."
        ),
    },
    "cell-chemistry": {
        "title": "Cell chemistry",
        "body": (
            "Halcyon Grid chose its chemistry for long life and supply security rather than energy "
            "density, because stationary storage is rarely limited by weight. The cells avoid cobalt "
            "and nickel entirely, which removes the most volatile inputs from the bill of materials. "
            "The trade-off is a larger footprint per megawatt-hour, which the company offsets with "
            "denser rack designs and simpler cooling. Halcyon cells use a sodium-iron phosphate "
            "chemistry. In accelerated testing the cells reached a cycle life of 9,200 full cycles "
            "before falling to 80 percent capacity."
        ),
    },
    "funding": {
        "title": "Funding history",
        "body": (
            "Halcyon Grid was funded initially by a seed round from angel investors with backgrounds "
            "in hydropower and grid software, followed by a Series A that paid for the pilot plant. "
            "Investors have generally favored capital efficiency, and the company has kept headcount "
            "low by contracting out non-core manufacturing steps. Grants from a regional innovation "
            "fund covered part of the early testing equipment. The Series B round raised $86 million. "
            "It was led by Fjord Ventures and closed in June 2025, with existing investors taking "
            "part."
        ),
    },
    "safety": {
        "title": "Safety and certification",
        "body": (
            "Safety work at Halcyon Grid covers cell-level abuse testing, module containment and "
            "site-level fire design. Every deployed system streams cell temperatures and voltages to "
            "a monitoring service that flags drift long before it becomes a hazard. The field team "
            "runs a quarterly drill with local fire services at each site, and incident reviews are "
            "shared with all customers. Halcyon systems passed UL 9540A large-scale fire testing in "
            "2025. The measured thermal incident rate across the fleet is 0.3 per million cell-hours."
        ),
    },
    "supply-chain": {
        "title": "Supply chain",
        "body": (
            "Halcyon Grid sources most of its materials from European suppliers to shorten logistics "
            "and reduce exposure to export controls. The company keeps dual sources for electrolyte "
            "and separators, and it holds eight weeks of safety stock for every critical part. "
            "Supplier quality is audited twice a year, and new suppliers go through a six-month "
            "qualification before reaching production. The cathode supplier is Kestrel Materials, "
            "based in Ostrava, Czech Republic. Cathode orders currently have a lead time of 11 weeks."
        ),
    },
    "roadmap": {
        "title": "Manufacturing roadmap",
        "body": (
            "Halcyon Grid plans to scale manufacturing in steps so that each new site repeats a proven "
            "line design instead of inventing a new one. The roadmap ties every expansion to signed "
            "offtake agreements, which keeps the company from building capacity ahead of demand. "
            "Automation is added where it improves quality first and throughput second. The second "
            "plant is called Bergen-2 and is planned to open in 2027. Bergen-2 is designed for an "
            "annual capacity of 1.6 GWh, roughly four times the pilot plant."
        ),
    },
    "pricing": {
        "title": "Pricing targets",
        "body": (
            "Halcyon Grid prices systems per kilowatt-hour of usable capacity, with service contracts "
            "sold separately on a per-year basis. Utilities usually compare offers on levelized cost "
            "of storage, so the company publishes its degradation assumptions alongside every quote. "
            "Long cycle life lets Halcyon compete on lifetime cost even when the upfront price is "
            "similar to rivals. The company targets a pack price of $71 per kWh in 2026, down from "
            "$98 per kWh in 2024."
        ),
    },
    "partnerships": {
        "title": "Utility partnerships",
        "body": (
            "Halcyon Grid works with utilities through staged pilots that start small and grow once "
            "availability and response times are proven. Each partnership includes shared access to "
            "operating data and a joint review of dispatch strategy every quarter. Utilities can "
            "convert a pilot into a full contract without retendering, which shortens the sales "
            "cycle considerably. The lead utility partner is Nordhavn Energi, which runs a 20 MW "
            "pilot installation that began operating in late 2025."
        ),
    },
    "recycling": {
        "title": "End-of-life and recycling",
        "body": (
            "Halcyon Grid designs modules to be taken apart with standard tools, so that cells can be "
            "removed, tested and either reused in second-life systems or sent to recycling. The "
            "company takes back every system it sells at end of life and covers the transport cost. "
            "A partner recycler processes returned cells using a low-temperature hydrometallurgical "
            "route. Current processing recovers 94 percent of the iron and 81 percent of the sodium "
            "compounds from returned cells."
        ),
    },
}
