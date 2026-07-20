from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional, TypeVar, overload, Union, List, Dict

from fda import lookup_ndc_package
from rxocrpl.quantities import Quantity


@dataclass
class Facility:
      name: str
      org: Optional[str] = None
      facility_type: Optional[str] = None
      admin_id: Optional[str] = None


@dataclass
class User:
      name: str
      org: Optional[Facility] = None
      user_type: Optional[str] = None


@dataclass
class ApprovedProductReconstitutionScheme:
      product_ndcs: List[str]
      facility: Facility
      user: User
      whole_product_strength: Quantity
      approved_diluents: List[str]
      diluent_volume_ml: Optional[float] = None
      final_volume_ml: Optional[float] = None
      verifying_rph: Optional[User] = None

      def final_concentration (self) -> Optional[Quantity]:
            if self.final_volume_ml is not None:
                  return self.whole_product_strength / self.final_volume_ml
            elif self.diluent_volume_ml is not None:
                  return self.whole_product_strength / (self.whole_product_strength / self.diluent_volume_ml)
            else:
                  return None

      def product_info (self):
            return [lookup_ndc_package(ndc) for ndc in self.product_ndcs]


if __name__ == "__main__":
      ncmc = Facility(name="NCMC", org="BHWR", facility_type="hospital", admin_id="NCMC-94")
      jts = User(name="Josh S", org=ncmc, user_type="CPHT")
      cspenser = User(name="Cassie Spenser", org=ncmc, user_type="RPh")

      recon_01 = ApprovedProductReconstitutionScheme(
            product_ndcs=["23155091431"],
            facility=ncmc,
            user=jts,
            whole_product_strength=Quantity(5_000_000, "[iU]"),
            approved_diluents=["SWFI", "NS"],
            diluent_volume_ml=3.2,
            final_volume_ml=5.0
      )

      print(recon_01)
      print(recon_01.product_info())
      print(recon_01.final_concentration())
