FROM nousresearch/hermes-agent@sha256:e0df6adebddf29b91112aefc999d4aaf6846c9eb544faca5672a16a13590ff79

COPY --chmod=0555 patch_weixin_media_ack.py /tmp/patch_weixin_media_ack.py
COPY --chmod=0555 patch_web_extract_single_url.py /tmp/patch_web_extract_single_url.py
RUN python /tmp/patch_weixin_media_ack.py /opt/hermes/gateway/platforms/weixin.py \
    && python /tmp/patch_web_extract_single_url.py /opt/hermes/tools/web_tools.py \
    && rm /tmp/patch_weixin_media_ack.py /tmp/patch_web_extract_single_url.py
