from pydantic import BaseModel, Field

from app.agent.interfaces import ModelClient
from app.contracts.api import (
    Classification,
    PaidSource,
    Plan,
    PlanLayer,
    SubQuestion,
    WeightAdjustment,
)


class PlanProposal(BaseModel):
    weight_adjustments: list[WeightAdjustment] = Field(default_factory=list)


TEMPLATE_DEFAULTS = {
    "coffee": {
        "layers": [
            (
                "demand_density",
                "Population density",
                "+",
                0.30,
                "Cafés need resident footfall; density is a low-cost proxy.",
            ),
            (
                "young_adults",
                "Young adult population",
                "+",
                0.15,
                "Young adults are a core café audience.",
            ),
            (
                "competition",
                "Existing cafés",
                "-",
                0.25,
                "Existing supply can indicate market saturation.",
            ),
            ("transit", "Transit access", "+", 0.15, "Transit supports visibility and access."),
            (
                "cost",
                "Commercial cost proxy",
                "-",
                0.15,
                "Lower cost improves the chance of viable operations.",
            ),
        ],
        "questions": [
            ("ihk", "IHK and trade association café reports", ["ihk.de", "berlin.de"]),
            ("district_concept", "District retail-centre concept", ["berlin.de"]),
            (
                "projects",
                "Upcoming residential and office projects",
                ["berlin.de", "tagesspiegel.de", "rbb24.de"],
            ),
            ("rents", "Commercial rent trends", []),
        ],
    },
    "kita": {
        "layers": [
            (
                "demand_density",
                "Population density",
                "+",
                0.35,
                "Population supports service-demand estimates.",
            ),
            (
                "young_adults",
                "Children and family proxy",
                "+",
                0.30,
                "Family-age population is a demand proxy.",
            ),
            (
                "competition",
                "Existing childcare supply",
                "-",
                0.20,
                "Existing supply can reduce unmet need.",
            ),
            ("transit", "Transit access", "+", 0.15, "Access matters for families."),
        ],
        "questions": [
            ("capacity", "Childcare capacity and district planning", ["berlin.de"]),
            ("projects", "Upcoming housing projects", ["berlin.de"]),
        ],
    },
}


async def plan(classification: Classification, model: ModelClient | None = None) -> Plan:
    template = classification.template if classification.template in TEMPLATE_DEFAULTS else "coffee"
    defaults = TEMPLATE_DEFAULTS[template]
    layers = [
        PlanLayer(id=id_, label=label, direction=direction, weight=weight, why=why)
        for id_, label, direction, weight, why in defaults["layers"]
    ]
    adjustments: list[WeightAdjustment] = []
    if model:
        try:
            proposed, _ = await model.structured(
                "planner",
                "plan",
                {
                    "classification": classification.model_dump(),
                    "layers": [layer.model_dump() for layer in layers],
                },
                PlanProposal,
            )
            for adjustment in proposed.weight_adjustments:
                layer = next((item for item in layers if item.id == adjustment.layer_id), None)
                if layer and adjustment.reason.strip() and adjustment.to_weight >= 0:
                    layer.weight = adjustment.to_weight
                    adjustments.append(adjustment)
        except Exception:
            pass
    total = sum(layer.weight for layer in layers)
    if total:
        for layer in layers:
            layer.weight /= total
    return Plan(
        layers=layers,
        sub_questions=[
            SubQuestion(id=id_, text=text, source_class="web", domains=domains)
            for id_, text, domains in defaults["questions"]
        ],
        paid_sources=[
            PaidSource(id="hystreet", name="hystreet.com"),
            PaidSource(id="google_places", name="Google Places"),
        ],
        weight_adjustments=adjustments,
    )
