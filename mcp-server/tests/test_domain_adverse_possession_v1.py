"""Test suite for Domain Profile adverse-possession/v1: extraction, schema serialization, and rite validation."""

import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from domain_profiles.adverse_possession_v1 import (
    AdversePossessionProfileData,
    ClaimantInfo,
    ConfrontanteItem,
    LandAreaInfo,
    PublicNoticeEntity,
    extract_adverse_possession_v1,
    validate_adverse_possession_v1,
)
from domain_rules import validate_domain_profile


class TestDomainAdversePossessionV1(unittest.TestCase):
    def test_model_serialization(self):
        """Test schema-first entity model instantiation and dict serialization."""
        claimant = ClaimantInfo(name="José da Silva", cpf_cnpj="111.222.333-44")
        land = LandAreaInfo(
            area_m2=200.0,
            perimeter_meters=50.0,
            memorial_descritivo_present=True,
        )
        conf = ConfrontanteItem(
            name="Carlos Almeida",
            cardinal_direction="norte",
            citation_status="citado_pessoalmente",
            manifestation_status="anuencia_expressa",
        )
        pub_u = PublicNoticeEntity(entity="União", notified=True, manifestation="sem_interesse")

        data = AdversePossessionProfileData(
            claimant=claimant,
            land_area=land,
            confrontantes={"confrontantes": [conf], "total_confrontantes": 1, "all_cited": True},
            public_notices={
                "uniao": pub_u,
                "estado": PublicNoticeEntity(entity="Estado do Pará", notified=True),
                "municipio": PublicNoticeEntity(entity="Município", notified=True),
                "all_notified": True,
            },
            possession_time_years_claimed=15.0,
            possession_type="extraordinaria",
        )

        d = data.model_dump()
        self.assertEqual(d["schema_version"], "adverse-possession/v1")
        self.assertEqual(d["claimant"]["name"], "José da Silva")
        self.assertEqual(d["land_area"]["area_m2"], 200.0)
        self.assertEqual(d["public_notices"]["uniao"]["manifestation"], "sem_interesse")

    def test_complete_adverse_possession_extraction(self):
        """Test extraction from raw petition documents."""
        documents = [
            {
                "id": "doc-10",
                "titulo": "Petição Inicial Usucapião",
                "tipo": "Petição Inicial",
                "texto": (
                    "Ação de Usucapião Especial Urbana de imóvel residencial com área de 250 m2 e perímetro de 60 m.\n"
                    "Posse há 15 anos ininterruptos e sem oposição.\n"
                    "Confrontante ao norte: Carlos Almeida, citado pessoalmente com anuência expressa.\n"
                    "Confrontante ao sul: Roberto Lima, citado por edital.\n"
                    "Planta topográfica e Memorial descritivo elaborado pelo Engenheiro Fernando Silva, ART n° 1234567-PA.\n"
                    "Notificação da União: União manifestou que não tem interesse na área.\n"
                    "Notificação do Estado do Pará: Fazenda Estadual notificada.\n"
                    "Notificação do Município: Prefeitura Municipal sem interesse."
                ),
            }
        ]

        res = extract_adverse_possession_v1(documents)

        self.assertEqual(res["schema_version"], "adverse-possession/v1")
        self.assertEqual(res["possession_type"], "urbana")
        self.assertEqual(res["possession_time_years_claimed"], 15.0)

        self.assertEqual(res["land_area"]["area_m2"], 250.0)
        self.assertEqual(res["land_area"]["perimeter_meters"], 60.0)
        self.assertTrue(res["land_area"]["memorial_descritivo_present"])
        self.assertEqual(res["land_area"]["technical_responsibility"]["art_rrt_number"], "1234567-PA")

        self.assertTrue(len(res["confrontantes"]["confrontantes"]) >= 2)
        self.assertTrue(res["confrontantes"]["all_cited"])
        self.assertFalse(res["confrontantes"]["any_contested"])

        self.assertEqual(res["public_notices"]["uniao"]["manifestation"], "sem_interesse")
        self.assertEqual(res["public_notices"]["municipio"]["manifestation"], "sem_interesse")

    def test_full_validation_on_compliant_adverse_possession(self):
        """Test validation when all citations and requirements are met."""
        documents = [
            {
                "id": "doc-20",
                "texto": (
                    "Ação de Usucapião Especial Urbana com área de 200 m2.\n"
                    "Posse contínua há 10 anos.\n"
                    "Confrontante ao norte: Carlos Almeida, citado pessoalmente.\n"
                    "Confrontante ao sul: Pedro Santos, citado pessoalmente.\n"
                    "Planta topográfica e Memorial descritivo com ART n° 998877-PA.\n"
                    "Notificação da União realizada, sem interesse.\n"
                    "Notificação do Estado do Pará realizada, sem interesse.\n"
                    "Notificação do Município realizada, sem interesse.\n"
                    "Citação do proprietário registral realizada."
                ),
            }
        ]
        profile_data = extract_adverse_possession_v1(documents)
        val_res = validate_adverse_possession_v1(profile_data)

        self.assertEqual(val_res.profile_name, "adverse-possession/v1")
        self.assertEqual(val_res.status, "COMPLETE")
        self.assertEqual(val_res.completeness_score, 1.0)
        self.assertEqual(len(val_res.missing_citations), 0)

        # Test via unified domain_rules dispatcher
        dossier = {"documents": documents}
        disp_res = validate_domain_profile(dossier, "adverse-possession/v1")
        self.assertEqual(disp_res["status"], "COMPLETE")
        self.assertEqual(disp_res["completeness_score"], 1.0)

    def test_validation_with_missing_confrontantes_citations(self):
        """Test validation when confrontantes citation is pending."""
        documents = [
            {
                "texto": (
                    "Usucapião extraordinária com posse há 15 anos.\n"
                    "Confrontante ao norte: João da Silva (pendente citação).\n"
                    "Memorial descritivo com ART n° 112233.\n"
                    "Notificação da União, Estado do Pará e Município realizadas."
                )
            }
        ]
        profile_data = extract_adverse_possession_v1(documents)
        val_res = validate_adverse_possession_v1(profile_data)

        self.assertIn(val_res.status, ("INCOMPLETE", "NON_COMPLIANT"))
        self.assertTrue(any("Confrontantes" in c for c in val_res.missing_citations))

    def test_insufficient_possession_time_for_modality(self):
        """Test validation when claimed possession time is below modality threshold."""
        documents = [
            {
                "texto": (
                    "Ação de Usucapião Extraordinária.\n"
                    "Autor possui o imóvel há 3 anos ininterruptos.\n"
                    "Confrontante ao norte: Carlos Almeida, citado pessoalmente.\n"
                    "Planta topográfica e Memorial descritivo com ART n° 123.\n"
                    "Notificação da União, Estado do Pará e Município efetuadas."
                )
            }
        ]
        profile_data = extract_adverse_possession_v1(documents)
        val_res = validate_adverse_possession_v1(profile_data)

        self.assertEqual(val_res.status, "NON_COMPLIANT")
        self.assertTrue(any("TEMPO_DE_POSSE_INSUFICIENTE" in r for r in val_res.procedural_risks))

    def test_area_limit_exceeded_for_urban_usucapion(self):
        """Test validation when urban usucapion area exceeds 250 m2 constitutional limit."""
        documents = [
            {
                "texto": (
                    "Ação de Usucapião Especial Urbana de imóvel residencial com área de 450 m2.\n"
                    "Posse há 6 anos.\n"
                    "Confrontantes citados. Fazendas notificadas. Memorial com ART 555."
                )
            }
        ]
        profile_data = extract_adverse_possession_v1(documents)
        val_res = validate_adverse_possession_v1(profile_data)

        self.assertEqual(val_res.status, "NON_COMPLIANT")
        self.assertTrue(any("EXCESSO_DE_AREA_URBANA" in r for r in val_res.procedural_risks))

    def test_extrajudicial_usucapion_opposition_non_compliant(self):
        """Test extrajudicial usucapion validation when public domain opposition exists."""
        documents = [
            {
                "texto": (
                    "Requerimento de Usucapião Extrajudicial perante o Cartório.\n"
                    "Estado do Pará opôs contestação alegando tratar-se de terreno de marinha público.\n"
                    "Ata notarial juntada."
                )
            }
        ]
        profile_data = extract_adverse_possession_v1(documents)
        val_res = validate_adverse_possession_v1(profile_data)

        self.assertEqual(val_res.status, "NON_COMPLIANT")
        self.assertTrue(any("INCOMPATIBILIDADE_EXTRAJUDICIAL" in r or "OPOSICAO" in r for r in val_res.procedural_risks))


if __name__ == "__main__":
    unittest.main()
