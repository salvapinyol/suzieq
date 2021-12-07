from ipaddress import ip_interface

from suzieq.engines.pandas.engineobj import SqPandasEngine
from suzieq.shared.utils import convert_macaddr_format_to_colon

import numpy as np
import pandas as pd


class AddressObj(SqPandasEngine):
    '''Backend to process/analyze Address table data'''

    @staticmethod
    def table_name():
        return 'address'

    def addr_type(self, addr: list) -> list:
        '''Return IP version of address, or 0 if MAC addr'''
        rslt = []
        for a in addr:
            try:
                ipa = ip_interface(a)
                rslt.append(ipa._version)  # pylint: disable=protected-access
            except ValueError:
                rslt.append(0)
        return rslt

    # pylint: disable=too-many-statements
    def get(self, **kwargs) -> pd.DataFrame:
        """Retrieve the dataframe that matches a given IPv4/v6/MAC address"""

        addr = kwargs.pop("address", [])
        prefix = kwargs.pop("prefix", [])
        columns = kwargs.get("columns", [])
        ipvers = kwargs.pop("ipvers", "")
        user_query = kwargs.pop("query_str", "")

        if user_query:
            if user_query.startswith('"') and user_query.endswith('"'):
                user_query = user_query[1:-1]

        vrf = kwargs.pop("vrf", "")
        addnl_fields = ['master']
        drop_cols = []

        if prefix:
            addr_types = self.addr_type(prefix)
        else:
            addr_types = self.addr_type(addr)

        # Always include ip or mac addresses in the dataframe
        # if there is a filter on them
        if columns not in [['default'], ['*']]:
            if ((4 in addr_types or ipvers == "v4") and
                    'ipAddressList' not in columns):
                addnl_fields.append('ipAddressList')
                drop_cols.append('ipAddressList')
            if ((6 in addr_types or ipvers == 'v6') and
                    'ip6AddressList' not in columns):
                addnl_fields.append('ip6AddressList')
                drop_cols.append('ip6AddressList')
            if ((0 in addr_types or ipvers == "l2") and
                    'macaddr' not in columns):
                addnl_fields.append('macaddr')
                drop_cols.append('macaddr')

        if vrf == "default":
            master = ''
        else:
            master = vrf
        df = self.get_valid_df("address", master=master,
                               addnl_fields=addnl_fields, **kwargs)

        if df.empty:
            return df

        if vrf == "default":
            df = df.query('master==""')

        df = df.rename({'master': 'vrf'}, axis=1) \
            .replace({'vrf': {'': 'default'}})

        if 4 in addr_types:
            df = df.explode('ipAddressList').fillna({'ipAddressList': ''})

        if 6 in addr_types:
            df = df.explode('ip6AddressList').fillna({'ip6AddressList': ''})

        if 'ipAddress' in columns or columns == ['*']:
            ndf = pd.DataFrame(df[['ipAddressList', 'ip6AddressList']].agg(
                self._merge_address_cols, axis=1),
                columns=['ipAddress'])
            df = pd.concat([df, ndf], axis=1)

        v4addr = []
        v6addr = []
        query_str = ''
        filter_prefix = ''

        # Address and prefix filtering are mutual exclusive
        if addr:
            macaddr = []
            for i, a in enumerate(addr):
                if addr_types[i] == 0:
                    # convert the macaddr format to internal format
                    if '.' in a:
                        a = convert_macaddr_format_to_colon(a)
                    macaddr.append(a.lower())
                elif addr_types[i] == 4:
                    if '/' not in a:
                        a += '/'
                    v4addr.append(a)
                elif addr_types[i] == 6:
                    if '/' not in a:
                        a += '/'
                    v6addr.append(a)

            # IMPORTANT: Don't mess with this order of query.
            # Some bug in pandas prevents it from working if
            # macaddr isn't first and your query
            # contains both a macaddr and an IP address.
            if macaddr:
                query_str += f'{filter_prefix} macaddr.isin({macaddr}) '
                filter_prefix = 'or'
            if v4addr:
                for a in v4addr:
                    query_str += (f'{filter_prefix} '
                                  f'ipAddressList.str.startswith("{a}") ')
                    filter_prefix = 'or'
            if v6addr:
                for a in v6addr:
                    query_str += (f'{filter_prefix} '
                                  f'ip6AddressList.str.startswith("{a}") ')
                    filter_prefix = 'or'

        elif prefix:
            for i, a in enumerate(prefix):
                if addr_types[i] == 4:
                    v4addr.append(a)
                elif addr_types[i] == 6:
                    v6addr.append(a)

            if v4addr:
                for a in v4addr:
                    query_str += (f'{filter_prefix} '
                                  f'@self._is_in_subnet(ipAddressList,"{a}")')
                    filter_prefix = 'or'
            if v6addr:
                for a in v6addr:
                    query_str += (f'{filter_prefix} '
                                  f'@self._is_in_subnet(ip6AddressList,"{a}")')
                    filter_prefix = 'or'

        if not query_str:
            if ipvers == "v4":
                query_str = 'ipAddressList.str.len() != 0'
            elif ipvers == "v6":
                query_str = 'ip6AddressList.str.len() != 0'
            elif ipvers == "l2":
                query_str = 'macaddr.str.len() != 0'

        if query_str:
            df = df.query(query_str)

        df = self._handle_user_query_str(df, user_query)
        return df.drop(columns=drop_cols)

    def unique(self, **kwargs) -> pd.DataFrame:
        """Specific here only if columns=vrf to filter out non-routed if"""
        columns = kwargs.get('columns', None)
        if columns == ['vrf']:
            query_str = kwargs.get('query_str', None)
            vrf_query_str = ('(ipAddressList.str.len() != 0 or '
                             'ip6AddressList.str.len() != 0)')
            if query_str:
                kwargs['query_str'] = f"{vrf_query_str} and {query_str}"
            else:
                kwargs['query_str'] = vrf_query_str
        return super().unique(**kwargs)

    def summarize(self, **kwargs) -> pd.DataFrame:
        """Summarize address related info"""

        self._init_summarize(**kwargs)
        if self.summary_df.empty:
            return self.summary_df

        self._add_field_to_summary('hostname', 'nunique', 'deviceCnt')
        self._add_field_to_summary('hostname', 'count', 'addressCnt')
        self._add_field_to_summary('macaddr', 'nunique', 'uniqueIfMacCnt')

        v6df = self.summary_df.explode('ip6AddressList') \
            .dropna(how='any') \
            .query('~ip6AddressList.str.startswith("fe80")')
        if not v6df.empty:
            v6device = v6df.groupby(by=['namespace'])['hostname'].nunique()
            v6addr = v6df.groupby(by=['namespace'])['ip6AddressList'].nunique()
            for i in self.ns.keys():
                self.ns[i].update(
                    {'deviceWithv6AddressCnt': v6device.get(i, 0)})
                self.ns[i].update({'uniqueV6AddressCnt': v6addr.get(i, 0)})
        else:
            for i in self.ns.keys():
                self.ns[i].update({'deviceWithv6AddressCnt': 0})
                self.ns[i].update({'uniqueV6AddressCnt': 0})

        v4df = self.summary_df.explode('ipAddressList') \
            .dropna(how='any') \
            .query('ipAddressList.str.len() != 0')
        if not v4df.empty:
            v4device = v4df.groupby(by=['namespace'])['hostname'].nunique()
            v4addr = v4df.groupby(by=['namespace'])['ipAddressList'].nunique()
            for i in self.ns.keys():
                self.ns[i].update(
                    {'deviceWithv4AddressCnt': v4device.get(i, 0)})
                self.ns[i].update({'uniqueV4AddressCnt': v4addr.get(i, 0)})

            v4df['prefixlen'] = v4df.ipAddressList.str.split('/').str[1]

            # this doesn't work if we've filtered by namespace
            #  pandas complains about an index problem
            #  so instead we have the more complicated expression below
            #  they are equivalent
            # v4pfx = v4df.groupby(by=['namespace'])['prefixlen'] \
            #             .value_counts().rename('count').reset_index()
            v4pfx = v4df.groupby(by=['namespace', 'prefixlen'],
                                 as_index=False)['ipAddressList'].count() \
                .dropna()
            v4pfx = v4pfx.rename(columns={'ipAddressList': 'count'})
            v4pfx['count'] = v4pfx['count'].astype(int)
            v4pfx = v4pfx.sort_values(by=['count'], ascending=False)

            for i in self.ns.keys():
                cnts = []
                v4pfx[v4pfx['namespace'] == i].apply(
                    lambda x: cnts.append(x['prefixlen']), axis=1, args=cnts)
                self.ns[i].update({'subnetsUsed': cnts})
                cnts = []
                v4pfx[v4pfx['namespace'] == i].apply(
                    lambda x: cnts.append({x['prefixlen']: x['count']}),
                    axis=1, args=cnts)
                self.ns[i].update({'subnetTopCounts': cnts[:3]})
        else:
            for i in self.ns.keys():
                self.ns[i].update({'deviceWithv4AddressCnt': 0})
                self.ns[i].update({'uniqueV4AddressCnt': 0})
                self.ns[i].update({'subnetsUsed': []})
                self.ns[i].update({'subnetTopCounts': []})

        self.summary_row_order = ['deviceCnt', 'addressCnt',
                                  'uniqueV4AddressCnt', 'uniqueV6AddressCnt',
                                  'uniqueIfMacCnt',
                                  'deviceWithv4AddressCnt',
                                  'deviceWithv6AddressCnt', 'subnetsUsed',
                                  'subnetTopCounts']
        self._post_summarize(check_empty_col='addressCnt')
        return self.ns_df.convert_dtypes()

    def _merge_address_cols(self, df: pd.DataFrame) -> np.array:
        res = np.append(np.array([]), df.values[0])
        return np.append(res, df.values[1])
