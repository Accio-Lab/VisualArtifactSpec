import unittest
from copy import deepcopy
from vas_bench.scoring import score
from vas_bench.aggregation import WEIGHTS, VERSION

def rubric():
    rows=[{'id':key,'dimension':'Task Fulfillment / Compliance','criterion':'Required task outcome',
           'answers':{'Yes':'Satisfied','No':'Not satisfied'},'compliance_groups':[group],
           'benchmark_source':'vas'} for key,group in [('A','G1'),('B','G1'),('C','G2')]]
    rows += [{'id':key,'dimension':dimension,'criterion':'Artifact quality','weight':1,
              'answers':{str(n):str(n) for n in range(-3,4)},'benchmark_source':'vas'}
             for key,dimension in zip(['C1','O1','V1','D1'],WEIGHTS)]
    return rows

def answers(rows,quality='0'):
    return {i['id']:{'answer':'Yes' if 'Yes' in i['answers'] else quality,'reason':'Observed evidence'} for i in rows}

class ScoringTests(unittest.TestCase):
    def test_full_fulfillment_passes_exactly_at_fifty_for_release_group_counts(self):
        for count in range(7, 26):
            with self.subTest(groups=count):
                source = rubric()
                rows = [{**deepcopy(source[0]), 'id': f'A{i}',
                         'compliance_groups': [f'G{i}']} for i in range(count)] + source[3:]
                result = score(rows, answers(rows))['unified']
                self.assertEqual(result['acceptance'], 50)
                self.assertEqual(result['total'], 50)
                self.assertTrue(result['passed'])
    def test_one_no_loses_whole_group_and_more_no_add_no_loss(self):
        rows=rubric();result=answers(rows);result['A']['answer']='No'
        self.assertEqual(score(rows,result)['unified']['acceptance'],25)
        result['B']['answer']='No'
        self.assertEqual(score(rows,result)['unified']['acceptance'],25)
    def test_group_size_and_item_weights_do_not_change_group_allocation(self):
        rows=rubric();rows[0]['weight']=100;result=answers(rows);result['A']['answer']='No'
        self.assertEqual(score(rows,result)['unified']['acceptance'],25)
    def test_quality_endpoints_and_signed_total(self):
        rows=rubric()
        for raw,points,total in [('-3',-50,0),('-2',-25,25),('-1',-10,40),
                                 ('0',0,50),('1',10,60),('2',25,75),('3',50,100)]:
            result=score(rows,answers(rows,raw))
            self.assertAlmostEqual(result['unified']['total'],total)
            self.assertAlmostEqual(result['unified']['quality'],points)
            self.assertEqual(result['items']['C1']['quality_points'],points)
            self.assertEqual(result['items']['C1']['normalized_100'],total)
            self.assertEqual(result['dimensions']['Content']['quality_points_mean'],points)
            self.assertEqual(result['unified']['version'],VERSION)
            self.assertEqual(result['schema_version'],2)
        result=answers(rows,'-3')
        for key in ['A','B','C']:result[key]['answer']='No'
        self.assertEqual(score(rows,result)['unified']['total'],-50)
    def test_map_each_item_before_weighted_mean(self):
        rows=rubric();rows[3]['weight']=1
        rows.append({**deepcopy(rows[3]),'id':'C2','weight':3})
        judgments=answers(rows);judgments['C2']['answer']='2'
        result=score(rows,judgments)
        # Content: (0*1 + 25*3)/4 = 18.75; its dimension weight is 25%.
        self.assertAlmostEqual(result['unified']['quality'],4.6875)
        self.assertAlmostEqual(result['unified']['total'],54.6875)
        self.assertAlmostEqual(result['dimensions']['Content']['normalized_100'],68.75)
    def test_dimension_weights_apply_to_mapped_points(self):
        rows=rubric();judgments=answers(rows)
        for key,value in [('C1','1'),('O1','2'),('V1','-1'),('D1','-2')]:
            judgments[key]['answer']=value
        result=score(rows,judgments)['unified']
        self.assertEqual(result['quality_contributions'],
                         {'Content':2.5,'Organization':3.75,'Visuals':-5,'Delivery':-2.5})
        self.assertAlmostEqual(result['total'],48.75)
    def test_missing_unknown_duplicate_and_invalid_group_fail(self):
        rows=rubric();result=answers(rows)
        del result['A']
        with self.assertRaises(ValueError):score(rows,result)
        result=answers(rows);result['A']['answer']='N/A'
        with self.assertRaises(ValueError):score(rows,result)
        rows[0].pop('compliance_groups')
        with self.assertRaises(ValueError):score(rows,answers(rows))
    def test_envelope_coverage(self):
        rows=rubric();result={'results':[{'id':key,**value} for key,value in answers(rows).items()]}
        self.assertEqual(score(rows,result)['unified']['total'],50)
        result['results'].append(deepcopy(result['results'][0]))
        with self.assertRaises(ValueError):score(rows,result)
