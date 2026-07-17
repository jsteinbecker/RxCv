from __future__ import print_function, annotations
import pint
from icecream import ic

r = pint.get_application_registry()
mg = lambda x: x * r.Unit("mg")
mL = lambda x: x * r.Unit("mL")

drug = mg(1000) / mL(10)
drug = drug.plus_minus(.1, relative=True)
ic(drug)

dil = mL(250).plus_minus(.1, relative=True)
ic(dil)

draw = mL(5).plus_minus(.05)  # withdrawn volume

dose = (drug * draw).to("mg")  # active drug delivered
final_volume = dil + draw  # bag + additive
final_conc = (dose / final_volume).to("mg/mL")  # label-claim concentration

ic(dose)  # (5.0 +/- 0.6)e+02 mg
ic(final_conc)  # (1.96 +/- 0.32) mg/mL  -> ~16.6% relative
