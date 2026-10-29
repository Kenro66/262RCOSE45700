"""Rebuild the existing #11 real-document test inputs from repository PDFs."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.ingestion.parser import parse_pdf


SOURCES = (
    ("DOC003", "中华人民共和国海关进出境行李物品监管办法.pdf", "response_DOC003.json"),
    ("DOC004", "《进境物品关税、增值税、消费税征收办法》.pdf", "response_1792900335398.json"),
    ("DOC005", "여행자 및 승무원 휴대품 통관에 관한 고시.pdf", "response_1792900378506.json"),
)


def prepare(data_dir: Path, output_dir: Path):
    # Parse every source before publishing any output; never invent a missing PDF.
    results = []
    for document_id, filename, fixture_name in SOURCES:
        path = data_dir / filename
        pages = parse_pdf(path)
        for page in pages:
            page.document_id = document_id
        results.append((fixture_name, {"pages": [p.model_dump() for p in pages]}, {
            "document_id": document_id, "filename": filename,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "pages": len(pages),
        }))
    output_dir.mkdir(parents=True, exist_ok=True)
    for filename, data, _ in results:
        (output_dir / filename).write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    manifest = [record for _, _, record in results]
    (output_dir / "source_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path(__file__).resolve().parents[2] / "data")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    manifest = prepare(args.data_dir, args.output_dir)
    print(json.dumps({"documents": len(manifest), "pages": sum(item["pages"] for item in manifest)}))


if __name__ == "__main__":
    main()
