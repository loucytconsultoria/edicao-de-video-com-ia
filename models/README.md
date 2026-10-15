# models/ — modelos de terceiros (não versionados)

Nenhum peso de modelo vem no repositório. Três modelos são usados:

| Modelo | Para quê | Como obter | Licença |
|---|---|---|---|
| **YuNet** (`face_detection_yunet_2023mar.onnx`, ~230 KB) | detecção de rosto: enquadramento 9:16 (`rostos.py`, `render_edl.py` modo `rosto`) e legenda fora do rosto (`checar_legenda_rosto.py`) | baixe de [opencv/opencv_zoo › models/face_detection_yunet](https://github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet) e coloque nesta pasta, ou aponte `VIDEO_IA_YUNET` para o arquivo | MIT (ver o README do modelo no opencv_zoo) |
| **Whisper** via faster-whisper (`small` … `large-v3`) | transcrição (`analisar.py`) e retranscrição local (`alinhar_fonetico.py`) | baixa sozinho no primeiro uso (cache do Hugging Face) | MIT (pesos do Whisper e faster-whisper) |
| **MMS_FA** (torchaudio) | alinhamento fonético forçado: a régua de tempo | baixa sozinho no primeiro uso (`torchaudio.pipelines.MMS_FA`) | os pesos do MMS são publicados para **uso não comercial (CC-BY-NC 4.0)** — confira os termos atuais antes de uso comercial; o motor isola o alinhador em `alinhar_fonetico.alinhar()` para facilitar a troca por um alinhador de licença permissiva |

SHA-256 do `face_detection_yunet_2023mar.onnx` usado no desenvolvimento:
`8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4`

Conferência rápida depois de baixar:

```bash
python scripts/rostos.py algum_video.mp4 --quadros 6   # "detector": "yunet"
```

Sem o YuNet, `rostos.py` tenta o Haar cascade do OpenCV (ausente no OpenCV 5)
e o modo `rosto` do render cai para o crop central, com aviso.
