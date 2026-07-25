from __future__ import annotations

import re
from collections import Counter
from typing import (
      Iterable,
      Iterator,
      Literal,
      Optional,
      Protocol,
      Sequence,
      TypeVar,
      Union,
      List,
      runtime_checkable, Dict,
)

from django.db import transaction
from django.db.models import Manager, Model, QuerySet
from typing_extensions import TypedDict

Bucket = Literal["rx", "otc", "unknown"]
Determination = Literal["clinical", "otc_cosmetic", "mixed", "indeterminate"]


@runtime_checkable
class ProductLike(Protocol):
      """Anything with the configured name attributes. Duck-typed, not a base class."""
      pk: object


@runtime_checkable
class LabelerLike(Protocol):
      pk: object


ProductT = TypeVar("ProductT", bound=Model)
LabelerT = TypeVar("LabelerT", bound=Model)

ProductSource = Union[QuerySet, Sequence[ProductLike], Iterable[ProductLike]]
LabelerSource = Union[QuerySet, Iterable[LabelerLike]]


class ClassificationResult(TypedDict):
      """Per-labeler classification output."""
      labeler: LabelerLike
      determination: Determination
      confidence: float
      rx_share: Optional[float]
      n_products: int
      rx_count: int
      otc_count: int
      unknown_count: int
      rx_fraction: float
      otc_fraction: float
      unknown_fraction: float
      rx_examples: list[str]
      otc_examples: list[str]
      unknown_examples: Optional[list[str]]


class LabelerClassifier:
      """
      Classifies Labeler querysets as clinical vs OTC/cosmetic based on their
      related products' substance names.
      """

      PRODUCT_RELATED_NAME: str = "product_set"
      PRODUCT_NAME_FIELDS: tuple[str, ...] = (
            "substance_name",
            "generic_name",
            "brand_name",
      )

      _RX_ONLY: frozenset[str] = frozenset({
            # cardio
            "atorvastatin", "simvastatin", "rosuvastatin", "pravastatin", "lovastatin", "ezetimibe",
            "lisinopril", "enalapril", "ramipril", "benazepril", "captopril", "quinapril", "fosinopril",
            "trandolapril", "losartan", "valsartan", "irbesartan", "olmesartan", "telmisartan",
            "candesartan", "sacubitril", "metoprolol", "carvedilol", "atenolol", "propranolol",
            "labetalol", "bisoprolol", "nadolol", "sotalol", "nebivolol", "amlodipine", "nifedipine",
            "felodipine", "diltiazem", "verapamil", "furosemide", "bumetanide", "torsemide",
            "hydrochlorothiazide", "chlorthalidone", "spironolactone", "eplerenone", "triamterene",
            "metolazone", "indapamide", "digoxin", "amiodarone", "flecainide", "propafenone",
            "mexiletine", "warfarin", "clopidogrel", "apixaban", "rivaroxaban", "prasugrel",
            "ticagrelor", "dabigatran", "enoxaparin", "heparin", "isosorbide", "nitroglycerin",
            "hydralazine", "clonidine", "prazosin", "doxazosin", "terazosin", "tamsulosin", "midodrine",
            # endocrine
            "metformin", "glipizide", "glyburide", "glimepiride", "pioglitazone", "sitagliptin",
            "alogliptin", "linagliptin", "empagliflozin", "dapagliflozin", "canagliflozin",
            "semaglutide", "liraglutide", "dulaglutide", "insulin", "levothyroxine", "liothyronine",
            "methimazole", "propylthiouracil", "alendronate", "raloxifene", "medroxyprogesterone",
            # neuro/psych
            "sertraline", "fluoxetine", "paroxetine", "citalopram", "escitalopram", "venlafaxine",
            "desvenlafaxine", "duloxetine", "bupropion", "mirtazapine", "trazodone", "amitriptyline",
            "nortriptyline", "doxepin", "imipramine", "clomipramine", "fluvoxamine", "buspirone",
            "quetiapine", "olanzapine", "risperidone", "aripiprazole", "ziprasidone", "haloperidol",
            "lurasidone", "clozapine", "lithium", "perphenazine", "fluphenazine", "thiothixene",
            "chlorpromazine", "trifluoperazine", "lamotrigine", "levetiracetam", "topiramate",
            "divalproex", "valproic", "carbamazepine", "oxcarbazepine", "phenytoin", "phenobarbital",
            "gabapentin", "pregabalin", "zonisamide", "lacosamide", "clonazepam", "lorazepam",
            "alprazolam", "diazepam", "temazepam", "zolpidem", "eszopiclone", "zaleplon", "ramelteon",
            "donepezil", "memantine", "galantamine", "rivastigmine", "carbidopa", "levodopa",
            "pramipexole", "ropinirole", "rasagiline", "selegiline", "amantadine", "benztropine",
            "sumatriptan", "rizatriptan", "zolmitriptan", "eletriptan", "baclofen", "tizanidine",
            "cyclobenzaprine", "methocarbamol", "carisoprodol", "metaxalone", "chlorzoxazone",
            "atomoxetine", "guanfacine", "methylphenidate", "dexmethylphenidate", "amphetamine",
            "dextroamphetamine", "lisdexamfetamine", "modafinil", "armodafinil", "phentermine",
            "diethylpropion", "phendimetrazine", "benzphetamine",
            # anti-infective
            "amoxicillin", "penicillin", "cephalexin", "cefdinir", "cefuroxime", "cefprozil",
            "cefadroxil", "cefpodoxime", "ceftriaxone", "cefazolin", "cefaclor", "cefixime",
            "azithromycin", "clarithromycin", "erythromycin", "doxycycline", "minocycline",
            "tetracycline", "ciprofloxacin", "levofloxacin", "moxifloxacin", "ofloxacin",
            "sulfamethoxazole", "trimethoprim", "nitrofurantoin", "metronidazole", "clindamycin",
            "vancomycin", "linezolid", "fluconazole", "itraconazole", "voriconazole", "posaconazole",
            "terbinafine", "acyclovir", "valacyclovir", "famciclovir", "oseltamivir", "isoniazid",
            "rifampin", "dapsone", "hydroxychloroquine", "chloroquine", "mefloquine", "ivermectin",
            "albendazole", "atovaquone", "lamivudine", "abacavir", "tenofovir", "raltegravir",
            "darunavir", "ritonavir", "doravirine", "maraviroc", "meropenem", "ertapenem",
            "piperacillin", "tazobactam", "daptomycin", "aztreonam", "tobramycin", "gentamicin",
            "amoxicillin and clavulanate", "dicloxacillin", "rifaximin", "nitazoxanide", "tinidazole",
            # onc / immune
            "paclitaxel", "docetaxel", "doxorubicin", "cyclophosphamide", "cisplatin", "oxaliplatin",
            "carboplatin", "gemcitabine", "fluorouracil", "capecitabine", "pemetrexed", "etoposide",
            "irinotecan", "methotrexate", "azacitidine", "decitabine", "bendamustine", "bortezomib",
            "lenalidomide", "imatinib", "dasatinib", "abiraterone", "anastrozole", "letrozole",
            "tamoxifen", "exemestane", "bicalutamide", "temozolomide", "mitomycin", "carmustine",
            "eribulin", "fulvestrant", "zoledronic", "leucovorin", "tacrolimus", "cyclosporine",
            "mycophenolate", "mycophenolic", "azathioprine", "leflunomide", "sirolimus", "adalimumab",
            "ustekinumab", "glatiramer", "dimethyl fumarate", "fingolimod", "teriparatide",
            "sulfasalazine", "mesalamine", "balsalazide",
            # other rx
            "pantoprazole", "rabeprazole", "dexlansoprazole", "ondansetron", "promethazine",
            "prochlorperazine", "metoclopramide", "dicyclomine", "hyoscyamine", "glycopyrrolate",
            "sucralfate", "oxybutynin", "tolterodine", "solifenacin", "trospium", "darifenacin",
            "mirabegron", "finasteride", "dutasteride", "sildenafil", "tadalafil", "vardenafil",
            "allopurinol", "colchicine", "probenecid", "febuxostat", "meloxicam", "celecoxib",
            "indomethacin", "etodolac", "nabumetone", "piroxicam", "sulindac", "ketorolac",
            "oxaprozin", "prednisone", "prednisolone", "methylprednisolone", "dexamethasone",
            "fludrocortisone", "hydroxyzine", "montelukast", "zafirlukast", "albuterol",
            "levalbuterol", "ipratropium", "tiotropium", "formoterol", "salmeterol", "beclomethasone",
            "cromolyn", "theophylline", "roflumilast", "benzonatate", "estradiol", "norethindrone",
            "norgestimate", "levonorgestrel", "drospirenone", "desogestrel", "progesterone",
            "testosterone", "calcitriol", "paricalcitol", "sevelamer", "cinacalcet", "ursodiol",
            "cholestyramine", "colesevelam", "fenofibrate", "gemfibrozil", "niacin", "icosapent",
            "naltrexone", "buprenorphine", "methadone", "morphine", "oxycodone", "hydrocodone",
            "hydromorphone", "oxymorphone", "codeine", "tramadol", "fentanyl", "butalbital",
            "clobetasol", "betamethasone", "triamcinolone", "fluocinonide", "fluocinolone",
            "desoximetasone", "halobetasol", "mometasone", "desonide", "flurandrenolide",
            "tretinoin", "adapalene", "tazarotene", "isotretinoin", "imiquimod", "mupirocin",
            "silver sulfadiazine", "latanoprost", "travoprost", "timolol", "dorzolamide",
            "brimonidine", "prednisolone acetate", "scopolamine", "meclizine",
            "epinephrine", "glucagon", "naloxone", "potassium chloride", "levocarnitine",
            "megestrol", "vaccine", "toxoid", "antigen", "immune globulin", "tuberculin",
      })

      _OTC_ONLY: frozenset[str] = frozenset({
            "loratadine", "cetirizine", "fexofenadine", "levocetirizine", "desloratadine", "pseudoephedrine",
            "dextromethorphan", "doxylamine", "chlorpheniramine", "brompheniramine", "triprolidine", "pheniramine", "nicotine",
            "omeprazole", "esomeprazole", "lansoprazole", "famotidine", "cimetidine", "ranitidine",
            "loperamide", "bismuth subsalicylate", "simethicone", "calcium carbonate", "docusate",
            "sennosides", "senna", "bisacodyl", "polyethylene glycol", "magnesium hydroxide",
            "magnesium citrate", "mineral oil", "glycerin", "psyllium", "methylcellulose",
            "calcium polycarbophil", "miconazole", "clotrimazole", "tioconazole", "terconazole",
            "butenafine", "tolnaftate", "undecylenic", "ketotifen", "olopatadine", "alcaftadine",
            "hydrocortisone", "benzoyl peroxide", "salicylic acid", "selenium sulfide",
            "pyrithione", "minoxidil", "docosanol", "permethrin", "pyrethrum", "piperonyl",
            "witch hazel", "petrolatum", "zinc oxide", "dimethicone", "calamine", "colloidal oatmeal",
            "pramoxine", "lidocaine", "benzocaine", "dibucaine", "menthol", "camphor", "capsaicin",
            "methyl salicylate", "trolamine", "eucalyptol", "thymol", "cetylpyridinium",
            "sodium fluoride", "stannous fluoride", "potassium nitrate", "sodium monofluorophosphate",
            "carbamide peroxide", "hydrogen peroxide", "isopropyl alcohol", "ethyl alcohol",
            "benzalkonium", "benzethonium", "chloroxylenol", "povidone-iodine", "iodine tincture",
            "bacitracin", "neomycin", "polymyxin", "avobenzone", "homosalate", "octisalate",
            "octocrylene", "octinoxate", "oxybenzone", "aluminum chlorohydrate", "epsom",
            "tetrahydrozoline", "naphazoline", "polyvinyl alcohol", "carboxymethylcellulose",
            "hypromellose", "dextran", "levmetamfetamine", "dimenhydrinate", "meclizine hydrochloride",
            "phenazopyridine", "magnesium salicylate", "salsalate", "caffeine", "ammonia", "tea tree",
            "bee venom", "arnica", "niacinamide", "hyaluronic", "ferric oxide", "castor oil",
            "sodium bicarbonate", "sodium chloride", "aluminum hydroxide", "zinc acetate",
            "zinc undecylenate", "resorcinol", "phenol", "urea", "silicone gel", "lanolin",
            "chlorhexidine", "sodium phosphate", "levonorgestrel", "ammonium lactate",
      })

      _COSMETIC_HINTS: tuple[str, ...] = (
            "shampoo", "soap", "wipes", "sunscreen", "moisturizing", "deodorant", "scar", "bandage",
            "wart", "corn remover", "skin tag", "milia", "numbing", "slimming", "booster", "balm",
            "toilet", "sanitizer", "remover", "serum", "essence", "bleach", "spray", "patches",
            "toothbrush", "dentifrice", "antacid", "laxative", "antifungal", "antipruritic",
            "first aid", "skin protectant", "liquid bandage", "hand ",
      )

      _STRENGTH_RE: re.Pattern[str] = re.compile(r"\b\d+(\.\d+)?\s*(%|mg|mcg|g|ml|w/w)\b")
      _PUNCT_RE: re.Pattern[str] = re.compile(r"[^a-z0-9 ]+")
      _WS_RE: re.Pattern[str] = re.compile(r"\s+")

      RX_CLINICAL_SHARE: float = 0.65
      RX_OTC_SHARE: float = 0.30
      UNKNOWN_MAX: float = 0.80

      @classmethod
      def _norm (cls, s: Optional[str]) -> str:
            if not s:
                  return ""
            s = cls._STRENGTH_RE.sub(" ", s.lower())
            s = cls._PUNCT_RE.sub(" ", s)
            return cls._WS_RE.sub(" ", s).strip()

      @classmethod
      def _product_text (cls, product: ProductLike) -> str:
            """Concatenate the configured name fields from a model instance."""
            parts: list[str] = []
            for field in cls.PRODUCT_NAME_FIELDS:
                  value: object = getattr(product, field, None)
                  if value:
                        parts.append(str(value))
            return " ".join(parts)

      @classmethod
      def classify_text (cls, text: Optional[str]) -> Bucket:
            """Return 'rx', 'otc', or 'unknown' for an arbitrary product string."""
            n: str = cls._norm(text)
            if not n:
                  return "unknown"

            rx_hit: bool = any(k in n for k in cls._RX_ONLY)
            otc_hit: bool = any(k in n for k in cls._OTC_ONLY)
            cos_hit: bool = any(h in n for h in cls._COSMETIC_HINTS)

            if rx_hit and otc_hit:
                  return "otc" if cos_hit else "rx"
            if rx_hit:
                  return "rx"
            if otc_hit or cos_hit:
                  return "otc"
            return "unknown"

      @classmethod
      def classify_product (cls, product: ProductLike) -> Bucket:
            """Return the bucket for a single Product model instance."""
            return cls.classify_text(cls._product_text(product))

      @classmethod
      def _score (
                cls,
                counts: Counter[Bucket],
                n_products: int,
                rx_examples: list[str],
                otc_examples: list[str],
                unknown_examples: Optional[list[str]] = None,
      ) -> ClassificationResult:
            n: int = max(n_products, 1)
            rx_f: float = counts["rx"] / n
            otc_f: float = counts["otc"] / n
            unk_f: float = counts["unknown"] / n

            known: float = rx_f + otc_f
            determination: Determination
            confidence: float
            rx_share: Optional[float]

            if known == 0 or unk_f > cls.UNKNOWN_MAX:
                  determination, confidence, rx_share = "indeterminate", 0.0, None
            else:
                  rx_share = rx_f / known
                  if rx_share >= cls.RX_CLINICAL_SHARE:
                        determination, confidence = "clinical", rx_share
                  elif rx_share <= cls.RX_OTC_SHARE:
                        determination, confidence = "otc_cosmetic", 1 - rx_share
                  else:
                        determination, confidence = "mixed", 1 - abs(rx_share - 0.5) * 2

            return ClassificationResult(
                  determination=determination,
                  confidence=round(confidence, 3),
                  rx_share=None if rx_share is None else round(rx_share, 3),
                  n_products=n_products,
                  rx_count=counts["rx"],
                  otc_count=counts["otc"],
                  unknown_count=counts["unknown"],
                  rx_fraction=round(rx_f, 3),
                  otc_fraction=round(otc_f, 3),
                  unknown_fraction=round(unk_f, 3),
                  rx_examples=rx_examples,
                  otc_examples=otc_examples,
                  unknown_examples=unknown_examples
            )

      @classmethod
      def classify_labeler (
                cls,
                labeler: LabelerLike,
                products: Optional[ProductSource] = None,
                example_limit: int = 5,
      ) -> ClassificationResult:
            """
            Classify a single Labeler instance.

            If `products` is supplied (a list/queryset already fetched), it is used
            directly — pass this when iterating a prefetched queryset to avoid N+1.
            """
            if products is None:
                  manager: Manager = getattr(labeler, cls.PRODUCT_RELATED_NAME)
                  products = manager.all()

            counts: Counter[Bucket] = Counter({"rx": 0, "otc": 0, "unknown": 0})
            rx_ex: list[str] = []
            otc_ex: list[str] = []
            unk_ex: list[str] = []
            total: int = 0

            for product in products:
                  total += 1
                  text: str = cls._product_text(product)
                  bucket: Bucket = cls.classify_text(text)
                  counts[bucket] += 1
                  if bucket == "rx" and len(rx_ex) < example_limit:
                        rx_ex.append(text)
                  elif bucket == "otc" and len(otc_ex) < example_limit:
                        otc_ex.append(text)
                  elif bucket == "unknown" and len(unk_ex) < example_limit:
                        unk_ex.append(text)

            return cls._score(counts, total, rx_ex, otc_ex, unk_ex)

      @classmethod
      def classify_queryset (
                cls,
                labelers: QuerySet[LabelerT] | Iterable[LabelerT],
                example_limit: int = 5,
      ) -> Dict[LabelerLike, ClassificationResult]:
            """
            Classify a Labeler queryset. Prefetches products in one extra query.

            Returns a dictionary mapping Labeler instances to ClassificationResult.
            """
            if isinstance(labelers, QuerySet):
                  labelers = labelers.prefetch_related(cls.PRODUCT_RELATED_NAME)

            results: Dict[LabelerLike, ClassificationResult] = {}
            for labeler in labelers:
                  manager: Manager = getattr(labeler, cls.PRODUCT_RELATED_NAME)
                  results[labeler] = cls.classify_labeler(
                        labeler,
                        products=list(manager.all()),
                        example_limit=example_limit,
                  )
            return results

      @classmethod
      def iter_queryset (
                cls,
                labelers: LabelerSource,
                chunk_size: int = 200,
                example_limit: int = 12,
      ) -> Iterator[tuple[LabelerLike, ClassificationResult]]:
            """
            Memory-friendly generator over a large Labeler queryset.

            Yields (labeler, ClassificationResult) pairs.
            """
            source: Iterable[LabelerLike]
            if isinstance(labelers, QuerySet):
                  source = labelers.prefetch_related(cls.PRODUCT_RELATED_NAME).iterator(
                        chunk_size=chunk_size
                  )
            else:
                  source = labelers

            for labeler in source:
                  manager: Manager = getattr(labeler, cls.PRODUCT_RELATED_NAME)
                  products: list[ProductLike] = list(manager.all())
                  yield labeler, cls.classify_labeler(
                        labeler, products=products, example_limit=example_limit
                  )

            # ---------------------------------------------------------------- persistence

            # ---------------------------------------------------------------- persistence

      @classmethod
      def persist (
                cls,
                labelers: LabelerSource,
                classifier_version: str = "v1",
                batch_size: int = 500,
                chunk_size: int = 200,
      ) -> int:
            """
            Classify and upsert results into LabelerClassification rows.

            Uses bulk_create with update_conflicts so re-running this is
            idempotent — existing rows for a labeler are overwritten rather
            than duplicated. Requires Django 4.1+ and a database backend that
            supports ON CONFLICT upserts (Postgres, SQLite 3.24+, MySQL 8+ via
            a different path — check your backend if this errors).

            Returns the number of rows written.
            """
            from auxil.models import LabelerClassification
            pending: list[LabelerClassification] = []
            written: int = 0

            update_fields: list[str] = [
                  "determination",
                  "confidence",
                  "rx_share",
                  "n_products",
                  "rx_count",
                  "otc_count",
                  "unknown_count",
                  "rx_fraction",
                  "otc_fraction",
                  "unknown_fraction",
                  "rx_examples",
                  "otc_examples",
                  "unknown_examples",
                  "classified_at",
                  "classifier_version",
            ]

            for labeler, result in cls.iter_queryset(labelers, chunk_size=chunk_size):
                  row: LabelerClassification = LabelerClassification.from_result(
                        labeler, result, classifier_version=classifier_version
                  )
                  pending.append(row)

                  if len(pending) >= batch_size:
                        written += cls._flush(pending, update_fields)
                        pending = []

            if pending:
                  written += cls._flush(pending, update_fields)

            return written

      @staticmethod
      def _flush (
                objs,
                update_fields: list[str],
      ) -> int:
            with transaction.atomic():
                  from auxil.models import LabelerClassification
                  LabelerClassification.objects.bulk_create(
                        objs,
                        update_conflicts=True,
                        update_fields=update_fields,
                        unique_fields=["labeler_id"],
                  )
            return len(objs)
