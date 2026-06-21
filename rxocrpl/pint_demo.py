from __future__ import print_function
from typing import List, Tuple, SupportsFloat
import sys
import pint

r = pint.get_application_registry()
mg = lambda x: x * r.Unit("mg")
mL = lambda x: x * r.Unit("mL")

drug = mg(1000) / mL(10)
drug = drug.plus_minus(.1, relative=True)
print(drug)
print(drug.__dict__)

dil = mL(250).plus_minus(.1, relative=True)
print(dil)

draw = mL(5).plus_minus(.05, relative=True)  # withdrawn volume

dose = (drug * draw).to("mg")  # active drug delivered
final_volume = dil + draw  # bag + additive
final_conc = (dose / final_volume).to("mg/mL")  # label-claim concentration

print(dose)  # (5.0 +/- 0.6)e+02 mg
print(final_conc)  # (1.96 +/- 0.32) mg/mL  -> ~16.6% relative
