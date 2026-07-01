from obm_quotation_helper import run_full_flow

email_body = """
Hi Ameera,

Kindly quote for these items.

Brand : SMC
Part No. : CDUJB10-6DM
Quantity : 2 PCS

Brand : SMC
Part No. : MXQ12L-75
Quantity : 2 PCS

Brand : SMC
Part No. : CXSJM6-20
Quantity : 2 PCS

Brand : SMC
Part No. : CDQ2B32-50DZ-XB14
Quantity : 2 PCS

Brand : SMC
Part No. : CDQ2B32-70DZ-XB14
Quantity : 2 PCS

Brand : SMC
Part No. : CDQ2B32-75DZ-XB14
Quantity : 2 PCS

Brand : SMC
Part No. : CDLQA100-50DC-F
Quantity : 1 PC

Brand : SMC
Part No. : CDQSB12-100DC
Quantity : 2 PCS

Brand : SMC
Part No. : MXQ8-20
Quantity : 2 PCS

Thanks,
image001.png
Ms Nurhidayah Aman
IPEX GLOBAL MANUFACTURING (M) SDN. BHD.
Purchasing Department.
Office +607-5225845
www.corp.i-pex.com
"""

items = [
    {
        "desc": "SMC CDUJB10-6DM",
        "qty": 2,
        "price": "0.00",
        "pid": "CDUJB10-6DM"
    },
    {
        "desc": "SMC MXQ12L-75",
        "qty": 2,
        "price": "0.00",
        "pid": "MXQ12L-75"
    },
    {
        "desc": "SMC CXSJM6-20",
        "qty": 2,
        "price": "0.00",
        "pid": "CXSJM6-20"
    },
    {
        "desc": "SMC CDQ2B32-50DZ-XB14",
        "qty": 2,
        "price": "0.00",
        "pid": "CDQ2B32-50DZ-XB14"
    },
    {
        "desc": "SMC CDQ2B32-70DZ-XB14",
        "qty": 2,
        "price": "0.00",
        "pid": "CDQ2B32-70DZ-XB14"
    },
    {
        "desc": "SMC CDQ2B32-75DZ-XB14",
        "qty": 2,
        "price": "0.00",
        "pid": "CDQ2B32-75DZ-XB14"
    },
    {
        "desc": "SMC CDLQA100-50DC-F",
        "qty": 1,
        "price": "0.00",
        "pid": "CDLQA100-50DC-F"
    },
    {
        "desc": "SMC CDQSB12-100DC",
        "qty": 2,
        "price": "0.00",
        "pid": "CDQSB12-100DC"
    },
    {
        "desc": "SMC MXQ8-20",
        "qty": 2,
        "price": "0.00",
        "pid": "MXQ8-20"
    }
]

if __name__ == "__main__":
    print("\n🚀 Running OBM Quotation Test for IPEX inquiry...\n")
    run_full_flow(email_body, items)