# Mainnet Migration Guide — Booppa Blockchain Anchoring

## Stato attuale
- Network: **Polygon Amoy Testnet** (costo gas = 0)
- Flag: `USE_MAINNET=False` (default, sicuro)
- Contract: `ANCHOR_CONTRACT_ADDRESS` su Amoy

## Quando migrare
Migrare a mainnet quando:
1. Il numero di clienti Enterprise attivi supera 20 (margine sufficiente a coprire il gas)
2. Un cliente o regolatore chiede esplicitamente un anchor "non testnet"
3. Si vuole aggiungere "Polygon Mainnet" come differenziatore commerciale

## Costo gas stimato
| Prodotto | Notarizzazioni/mese | Costo gas max (MATIC) | Costo USD (~$0.80/MATIC) |
|---|---|---|---|
| Standard Suite (50 notar.) | 50 | 0.5 MATIC | $0.40 |
| Pro Suite (100 notar.) | 100 | 1.0 MATIC | $0.80 |
| Compliance Bundle (one-time) | 3 | 0.03 MATIC | $0.024 |
| **Totale 20 clienti Enterprise** | **~1.060** | **~10.6 MATIC** | **~$8.50/mese** |

Costo mensile totale a 20 clienti Enterprise: **< SGD 12/mese**. Assorbibile nel margine di Standard Suite (SGD 1.800/mese × 20 = SGD 36.000 ricavi).

## Checklist migrazione (da completare in ordine)

### Fase 1 — Preparazione (1-2 giorni dev)
- [ ] Eseguire `apply_fixes.sh` (aggiunge `USE_MAINNET` flag e `active_polygon_*` properties)
- [ ] Deploy del contratto `EvidenceAnchorV3` su Polygon Mainnet
- [ ] Salvare l'indirizzo del contratto mainnet in `POLYGON_MAINNET_CONTRACT_ADDRESS`
- [ ] Acquistare 10 MATIC (~$8 USD) come riserva iniziale
- [ ] Configurare `BLOCKCHAIN_PRIVATE_KEY` (wallet con saldo MATIC)

### Fase 2 — Test (1 giorno)
- [ ] In ambiente staging: impostare `USE_MAINNET=True` nel `.env`
- [ ] Generare 3 anchor di test, verificare su [polygonscan.com](https://polygonscan.com)
- [ ] Verificare che `active_polygon_explorer_url` nei PDF punti a `polygonscan.com`
- [ ] Verificare che il notice nei PDF riporti "Polygon Mainnet" (non "Amoy Testnet")
- [ ] Testare un ciclo completo: PDPA scan → anchor → Cover Sheet → email

### Fase 3 — Switch produzione
- [ ] Aggiornare `.env` in produzione: `USE_MAINNET=True`
- [ ] Aggiornare `POLYGON_MAINNET_CONTRACT_ADDRESS` con l'indirizzo mainnet
- [ ] Riavviare i Celery workers: `supervisorctl restart celery:*`
- [ ] Monitorare i log per errori di gas nei prossimi 30 minuti

### Fase 4 — Comunicazione clienti
- [ ] Email a tutti i clienti con abbonamento attivo:
  > "A partire da [data], tutti i nuovi documenti Booppa sono ancorati su Polygon Mainnet 
  > — il blockchain principale. I documenti precedenti rimangono su Amoy Testnet 
  > e rimangono verificabili su [amoy.polygonscan.com](https://amoy.polygonscan.com)."
- [ ] Aggiornare il copy commerciale: rimuovere "(Testnet)" dalla descrizione dei prodotti
- [ ] Aggiornare la FAQ su booppa.io

## Strategia di risparmio gas (batch anchoring)

Per clienti ad alto volume (Standard/Pro Suite), il batch anchoring riduce i costi del ~90%:

```python
# Invece di anchorare ogni documento singolarmente:
for doc in documents:  # 50 chiamate RPC
    await anchor_evidence(doc.hash)

# Usare Merkle root (1 chiamata RPC per N documenti):
import hashlib

def merkle_root(hashes: list[str]) -> str:
    """Compute the Merkle root of a list of SHA-256 hashes."""
    if not hashes:
        return ""
    layer = [h.encode() for h in sorted(hashes)]
    while len(layer) > 1:
        if len(layer) % 2 == 1:
            layer.append(layer[-1])  # duplicate last node
        layer = [
            hashlib.sha256(layer[i] + layer[i+1]).hexdigest().encode()
            for i in range(0, len(layer), 2)
        ]
    return layer[0].decode()

# Schedulare un anchor giornaliero per tutti i documenti del giorno:
# 50 documenti/giorno × 30 giorni = 1 tx/giorno invece di 50 tx/giorno
daily_root = merkle_root([doc.hash for doc in today_documents])
tx_hash = await blockchain.anchor_evidence(daily_root, metadata=f"batch:{date}")
```

Implementare questa ottimizzazione prima del lancio mainnet se il volume mensile supera 200 documenti.

## Rollback
In caso di problemi post-switch:
```bash
# In produzione, reverting a testnet è istantaneo:
echo "USE_MAINNET=False" >> .env
supervisorctl restart celery:*
```
I documenti ancorati su mainnet rimangono validi — solo i nuovi anchor torneranno su testnet.
